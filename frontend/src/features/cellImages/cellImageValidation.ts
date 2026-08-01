// Validation for a file dropped onto a spreadsheet cell. This module decides whether a payload may
// become an in-cell image, and it is the only place in the feature that inspects a file's contents.
//
// WHY METADATA IS NOT ENOUGH, WHICH IS THE WHOLE REASON THIS MODULE EXISTS. A dropped File carries two
// pieces of metadata that a page is tempted to trust: `type` and `size`. Both are supplied by whatever
// produced the file, and neither describes what a decoder will actually do with the bytes.
//   * `type` is a label, not a fact. A vector image renamed to end in .png is handed over as
//     `image/png`; an allow-list check on `type` alone therefore admits a file class this feature has
//     deliberately excluded. Reading the container's own signature is what turns the label into a
//     verified claim.
//   * `size` is the COMPRESSED length. Every raster container stores its canvas size in a header, so a
//     few dozen bytes can legitimately declare a surface of thousands of pixels on a side, which a
//     decoder will then allocate in full. A file that passes a ten-mebibyte ceiling can therefore cost
//     tens of mebibytes of decoded memory, and a handful of such files can cost hundreds. Bounding the
//     DECLARED surface before anything decodes it, and the DECODED surface before anything is retained,
//     is what closes that gap.
//
// ORDER OF THE GATES, AND WHY THE ORDER IS THE POINT. Cheapest and most conclusive first, so that the
// expensive step is never reached by a payload that a free check could have refused:
//   1. metadata      allow-list, non-empty, per-file byte ceiling      no I/O at all
//   2. container     signature match plus declared size and frame count  one bounded, transient read
//   3. ceilings      width, height, pixels, decoded bytes, frames      arithmetic on step 2
//   4. decode        the surface a real decoder reports                 the only step that costs memory
//   5. ceilings      re-applied to the measured surface                 arithmetic on step 4
// Step 3 running BEFORE step 4 is the load-bearing detail: a file whose header declares an enormous
// canvas is refused without ever being decoded, so the expansion it was built to trigger never happens.
// Only a file whose declared surface already fits the ceilings is decoded at all, which bounds the
// transient cost of step 4 to one image within the per-image budget.
//
// NOTHING HERE MINTS A PERMANENT RESOURCE. The store mints the object URL that an accepted image is
// rendered from, and it does so only after this module has approved the file. The fallback decode path
// needs a temporary URL of its own; it is revoked on every outcome, including a timeout, so a probe can
// never pin a blob. No cell data is read or written, no state is held between calls, and every function
// here is either pure or bounded by exactly one file.

import type { CellImageEntry, CellImageRejectionReason } from '../../types/cellImage';
import {
  ACCEPTED_IMAGE_MIME_TYPES,
  DECODED_BYTES_PER_PIXEL,
  DECODE_PROBE_TIMEOUT_MS,
  IMAGE_HEADER_PROBE_BYTES,
  MAX_DECODED_IMAGE_BYTES,
  MAX_IMAGE_BYTES,
  MAX_IMAGE_FRAMES,
  MAX_IMAGE_HEIGHT,
  MAX_IMAGE_PIXELS,
  MAX_IMAGE_WIDTH,
} from './cellImageTokens';

// The measurements an accepted file contributes to its entry. Derived from CellImageEntry with Pick so
// the probe and the entry cannot drift apart, and so the feature keeps exactly one shared shape.
export type CellImageProbe = Pick<
  CellImageEntry,
  'pixelWidth' | 'pixelHeight' | 'decodedBytes' | 'frameCount'
>;

// A discriminated union rather than a nullable value: a caller cannot read the measurements without
// having first narrowed on success, and cannot reach a rejection without a reason to report.
export type CellImageProbeResult =
  | { ok: true; probe: CellImageProbe }
  | { ok: false; reason: CellImageRejectionReason };

// What a container's own bytes say about itself. A pixelWidth or pixelHeight of zero means the bytes
// read did not contain the declaration — possible for a JPEG whose frame header sits beyond the prefix
// this module reads — in which case the decode step has to supply the surface instead.
interface ContainerMeasurement {
  pixelWidth: number;
  pixelHeight: number;
  frameCount: number;
}

type ContainerReading =
  | { ok: true; measurement: ContainerMeasurement }
  | { ok: false; reason: CellImageRejectionReason };

// The three outcomes of asking the platform to decode a file: it reported a surface, it refused the
// bytes, or it offers no way to ask. The third is a capability fact, not a failure, and is handled by
// falling back to the container's own declaration.
type DecodedSurface =
  | { status: 'measured'; width: number; height: number }
  | { status: 'undecodable' }
  | { status: 'unavailable' };

// Containers that can hold more than one frame. These are read in full because a frame count cannot be
// determined from a prefix; the others are read only up to IMAGE_HEADER_PROBE_BYTES. This is a fact
// about the formats rather than a tunable policy, so it lives beside the parsers that rely on it.
const ANIMATION_CAPABLE_MIME_TYPES = ['image/gif', 'image/png', 'image/webp'] as const;

// Container signatures, as byte sequences rather than strings, because that is how they appear in a
// file and because a byte comparison cannot be confused by text encoding.
const PNG_SIGNATURE = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a] as const;
const JPEG_SIGNATURE = [0xff, 0xd8, 0xff] as const;
const GIF87A_SIGNATURE = [0x47, 0x49, 0x46, 0x38, 0x37, 0x61] as const;
const GIF89A_SIGNATURE = [0x47, 0x49, 0x46, 0x38, 0x39, 0x61] as const;
const BMP_SIGNATURE = [0x42, 0x4d] as const;
const RIFF_SIGNATURE = [0x52, 0x49, 0x46, 0x46] as const;

// Structural offsets and markers, named so the parsers below read as format descriptions rather than
// as arithmetic. Each is fixed by the format's specification and none is a policy decision.
const PNG_FIRST_CHUNK_OFFSET = 8;
const PNG_CHUNK_HEADER_BYTES = 8;
const PNG_IHDR_TYPE_OFFSET = 12;
const PNG_IHDR_WIDTH_OFFSET = 16;
const PNG_IHDR_HEIGHT_OFFSET = 20;
const PNG_MINIMUM_BYTES = 33;
const JPEG_HEADER_MARKER = 0xff;
const JPEG_START_OF_SCAN = 0xda;
const JPEG_START_OF_IMAGE = 0xd8;
const JPEG_STANDALONE_FIRST = 0xd0;
const JPEG_STANDALONE_LAST = 0xd9;
const JPEG_TEM_MARKER = 0x01;
const GIF_HEADER_BYTES = 13;
const GIF_SCREEN_WIDTH_OFFSET = 6;
const GIF_SCREEN_HEIGHT_OFFSET = 8;
const GIF_SCREEN_FLAGS_OFFSET = 10;
const GIF_EXTENSION_INTRODUCER = 0x21;
const GIF_IMAGE_SEPARATOR = 0x2c;
const GIF_TRAILER = 0x3b;
const GIF_IMAGE_DESCRIPTOR_BYTES = 10;
const GIF_COLOR_TABLE_FLAG = 0x80;
const GIF_COLOR_TABLE_SIZE_MASK = 0x07;
const BMP_HEADER_SIZE_OFFSET = 14;
const BMP_CORE_HEADER_BYTES = 12;
const BMP_CORE_WIDTH_OFFSET = 18;
const BMP_CORE_HEIGHT_OFFSET = 20;
const BMP_INFO_WIDTH_OFFSET = 18;
const BMP_INFO_HEIGHT_OFFSET = 22;
const BMP_MINIMUM_BYTES = 26;
const WEBP_FORM_TYPE_OFFSET = 8;
const WEBP_FIRST_CHUNK_OFFSET = 12;
const WEBP_CHUNK_HEADER_BYTES = 8;
const WEBP_VP8L_SIGNATURE = 0x2f;
const WEBP_DIMENSION_MASK = 0x3fff;

// Reads a big-endian 32-bit value. Written with multiplication rather than a shift because a shift
// operates on a signed 32-bit integer in JavaScript and would turn a large length into a negative one.
const readU32BE = (bytes: Uint8Array, offset: number): number =>
  bytes[offset] * 0x1000000 + bytes[offset + 1] * 0x10000 + bytes[offset + 2] * 0x100 + bytes[offset + 3];

const readU16BE = (bytes: Uint8Array, offset: number): number => bytes[offset] * 0x100 + bytes[offset + 1];

const readU16LE = (bytes: Uint8Array, offset: number): number => bytes[offset] + bytes[offset + 1] * 0x100;

const readU24LE = (bytes: Uint8Array, offset: number): number =>
  bytes[offset] + bytes[offset + 1] * 0x100 + bytes[offset + 2] * 0x10000;

const readU32LE = (bytes: Uint8Array, offset: number): number =>
  bytes[offset] + bytes[offset + 1] * 0x100 + bytes[offset + 2] * 0x10000 + bytes[offset + 3] * 0x1000000;

// Signed variant for the BMP information header, whose height is negative when rows are stored
// top-down. The sign carries row order, not magnitude, so callers take the absolute value.
const readI32LE = (bytes: Uint8Array, offset: number): number => {
  const value = readU32LE(bytes, offset);
  return value > 0x7fffffff ? value - 0x100000000 : value;
};

// True when the bytes at the given offset are exactly the signature. Returns false rather than throwing
// when the buffer is too short, so a truncated file is a mismatch instead of an exception.
const matchesSignature = (
  bytes: Uint8Array,
  signature: readonly number[],
  offset: number,
): boolean => {
  if (bytes.length < offset + signature.length) {
    return false;
  }
  return signature.every((value, index) => bytes[offset + index] === value);
};

// Reads a fixed-length ASCII tag such as a PNG chunk type or a RIFF four-character code. Returns an
// empty string when the buffer is too short, which every caller treats as "no tag here".
const readTag = (bytes: Uint8Array, offset: number, length: number): string => {
  if (bytes.length < offset + length) {
    return '';
  }
  let tag = '';
  for (let index = 0; index < length; index += 1) {
    tag += String.fromCharCode(bytes[offset + index]);
  }
  return tag;
};

// Reads the bytes of a blob into a plain byte array.
//
// Blob.arrayBuffer is the direct route and is used wherever the platform provides it. FileReader is the
// fallback for platforms that do not, and it reads into an ArrayBuffer — which is emphatically NOT the
// base64 data-URL idiom this feature avoids. That idiom (readAsDataURL) inflates a payload by about a
// third and would become the image's render source; these bytes are a transient copy used to verify a
// container and are dropped as soon as the parse returns, while the render source remains the object URL
// the store mints. The read is bounded by the per-file ceiling that has already been enforced, and by
// IMAGE_HEADER_PROBE_BYTES for containers that cannot animate.
const readBlobBytes = async (blob: Blob): Promise<Uint8Array> => {
  if (typeof blob.arrayBuffer === 'function') {
    return new Uint8Array(await blob.arrayBuffer());
  }
  const buffer = await new Promise<ArrayBuffer>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = (): void => {
      const result = reader.result;
      if (result instanceof ArrayBuffer) {
        resolve(result);
        return;
      }
      reject(new Error('The dropped file could not be read as bytes.'));
    };
    reader.onerror = (): void => {
      reject(reader.error ?? new Error('The dropped file could not be read.'));
    };
    reader.readAsArrayBuffer(blob);
  });
  return new Uint8Array(buffer);
};

// PNG: an eight-byte signature, then length-prefixed chunks. IHDR must be the first chunk and carries
// the canvas size; an animated PNG adds acTL, which declares a frame count, and one fcTL per frame.
// Both frame sources are consulted and the larger wins, because a file may declare more frames than it
// actually stores and the ceiling should judge the claim as well as the content.
const readPngContainer = (bytes: Uint8Array): ContainerReading => {
  if (!matchesSignature(bytes, PNG_SIGNATURE, 0)) {
    return { ok: false, reason: 'format-mismatch' };
  }
  if (bytes.length < PNG_MINIMUM_BYTES || readTag(bytes, PNG_IHDR_TYPE_OFFSET, 4) !== 'IHDR') {
    return { ok: false, reason: 'undecodable' };
  }
  const pixelWidth = readU32BE(bytes, PNG_IHDR_WIDTH_OFFSET);
  const pixelHeight = readU32BE(bytes, PNG_IHDR_HEIGHT_OFFSET);
  if (pixelWidth === 0 || pixelHeight === 0) {
    return { ok: false, reason: 'undecodable' };
  }

  let frameChunks = 0;
  let declaredFrames = 0;
  let offset = PNG_FIRST_CHUNK_OFFSET;
  while (offset + PNG_CHUNK_HEADER_BYTES <= bytes.length) {
    const length = readU32BE(bytes, offset);
    const type = readTag(bytes, offset + 4, 4);
    const dataStart = offset + 8;
    // A chunk that claims more bytes than remain means the walk has reached the end of what was read,
    // whether because the file is truncated or because only a prefix was requested. Stop and keep the
    // counts gathered so far rather than reading past the buffer.
    if (dataStart + length + 4 > bytes.length) {
      break;
    }
    if (type === 'IEND') {
      break;
    }
    if (type === 'acTL' && length >= 4) {
      declaredFrames = readU32BE(bytes, dataStart);
    } else if (type === 'fcTL') {
      frameChunks += 1;
    }
    offset = dataStart + length + 4;
  }

  return {
    ok: true,
    measurement: {
      pixelWidth,
      pixelHeight,
      frameCount: Math.max(frameChunks, declaredFrames, 1),
    },
  };
};

// JPEG frame headers, whose payload begins with one byte of sample precision followed by height then
// width. Progressive, extended and arithmetic-coded variants each have their own marker, and all of
// them describe the frame the same way; the differential markers are excluded because they describe a
// difference from a previous frame rather than a canvas.
const isJpegFrameMarker = (marker: number): boolean =>
  (marker >= 0xc0 && marker <= 0xc3) ||
  (marker >= 0xc5 && marker <= 0xc7) ||
  (marker >= 0xc9 && marker <= 0xcb) ||
  (marker >= 0xcd && marker <= 0xcf);

// JPEG: a marker-segment stream. The canvas size lives in the frame header, which sits after any
// metadata segments, so the segment chain is walked until a frame header is found or the compressed
// data begins. A file whose frame header lies beyond the prefix that was read yields a zero-sized
// measurement, and the decode step supplies the surface instead.
const readJpegContainer = (bytes: Uint8Array): ContainerReading => {
  if (!matchesSignature(bytes, JPEG_SIGNATURE, 0)) {
    return { ok: false, reason: 'format-mismatch' };
  }

  let offset = 2;
  while (offset + 1 < bytes.length) {
    if (bytes[offset] !== JPEG_HEADER_MARKER) {
      // Not aligned on a marker: the stream is not the segment sequence a JPEG must be.
      return { ok: false, reason: 'undecodable' };
    }
    const marker = bytes[offset + 1];
    // Padding between segments is written as repeated marker bytes.
    if (marker === JPEG_HEADER_MARKER) {
      offset += 1;
      continue;
    }
    // Standalone markers carry no payload and no length.
    if (
      marker === JPEG_START_OF_IMAGE ||
      marker === JPEG_TEM_MARKER ||
      (marker >= JPEG_STANDALONE_FIRST && marker <= JPEG_STANDALONE_LAST)
    ) {
      offset += 2;
      continue;
    }
    if (offset + 4 > bytes.length) {
      break;
    }
    const segmentLength = readU16BE(bytes, offset + 2);
    // A segment length counts its own two bytes, so anything below two is malformed.
    if (segmentLength < 2) {
      return { ok: false, reason: 'undecodable' };
    }
    if (isJpegFrameMarker(marker)) {
      if (offset + 9 > bytes.length) {
        break;
      }
      const pixelHeight = readU16BE(bytes, offset + 5);
      const pixelWidth = readU16BE(bytes, offset + 7);
      if (pixelWidth === 0 || pixelHeight === 0) {
        return { ok: false, reason: 'undecodable' };
      }
      return { ok: true, measurement: { pixelWidth, pixelHeight, frameCount: 1 } };
    }
    // Compressed data starts here, so no frame header will be found beyond this point.
    if (marker === JPEG_START_OF_SCAN) {
      break;
    }
    offset += 2 + segmentLength;
  }

  return { ok: true, measurement: { pixelWidth: 0, pixelHeight: 0, frameCount: 1 } };
};

// Advances past a GIF data sub-block chain: length-prefixed blocks terminated by a zero length. Returns
// the offset just past the terminator, or -1 when the bytes run out mid-chain.
const skipGifSubBlocks = (bytes: Uint8Array, start: number): number => {
  let offset = start;
  while (offset < bytes.length) {
    const size = bytes[offset];
    offset += 1;
    if (size === 0) {
      return offset;
    }
    offset += size;
  }
  return -1;
};

// A GIF colour table's byte length: three bytes per entry, with the entry count encoded as a power of
// two in the low bits of the packed field.
const gifColorTableBytes = (packedField: number): number =>
  (packedField & GIF_COLOR_TABLE_FLAG) !== 0
    ? 3 * (1 << ((packedField & GIF_COLOR_TABLE_SIZE_MASK) + 1))
    : 0;

// GIF: a fixed header whose logical screen size is the canvas a decoder allocates, followed by a block
// chain in which each image descriptor introduces one frame. The logical screen size is exactly the
// field that lets a tiny file demand an enormous surface, which is why it — and not the size of any
// individual frame — is the measurement this parser reports.
const readGifContainer = (bytes: Uint8Array): ContainerReading => {
  if (
    !matchesSignature(bytes, GIF87A_SIGNATURE, 0) &&
    !matchesSignature(bytes, GIF89A_SIGNATURE, 0)
  ) {
    return { ok: false, reason: 'format-mismatch' };
  }
  if (bytes.length < GIF_HEADER_BYTES) {
    return { ok: false, reason: 'undecodable' };
  }
  const pixelWidth = readU16LE(bytes, GIF_SCREEN_WIDTH_OFFSET);
  const pixelHeight = readU16LE(bytes, GIF_SCREEN_HEIGHT_OFFSET);
  if (pixelWidth === 0 || pixelHeight === 0) {
    return { ok: false, reason: 'undecodable' };
  }

  let frameCount = 0;
  let offset = GIF_HEADER_BYTES + gifColorTableBytes(bytes[GIF_SCREEN_FLAGS_OFFSET]);
  while (offset < bytes.length) {
    const block = bytes[offset];
    if (block === GIF_TRAILER) {
      break;
    }
    if (block === GIF_EXTENSION_INTRODUCER) {
      // Introducer plus label, then the extension's own sub-block chain.
      const next = skipGifSubBlocks(bytes, offset + 2);
      if (next < 0) {
        break;
      }
      offset = next;
      continue;
    }
    if (block === GIF_IMAGE_SEPARATOR) {
      frameCount += 1;
      if (offset + GIF_IMAGE_DESCRIPTOR_BYTES > bytes.length) {
        break;
      }
      const localFlags = bytes[offset + 9];
      // Descriptor, optional local colour table, then one byte of LZW minimum code size before the
      // frame's own sub-block chain.
      const imageDataStart =
        offset + GIF_IMAGE_DESCRIPTOR_BYTES + gifColorTableBytes(localFlags) + 1;
      const next = skipGifSubBlocks(bytes, imageDataStart);
      if (next < 0) {
        break;
      }
      offset = next;
      continue;
    }
    // An unexpected block introducer means the chain cannot be walked further; stop with what has been
    // counted rather than guessing at a resynchronisation point.
    break;
  }

  if (frameCount === 0) {
    // A GIF with no image descriptor has nothing to render, so there is no surface to account for.
    return { ok: false, reason: 'undecodable' };
  }

  return { ok: true, measurement: { pixelWidth, pixelHeight, frameCount } };
};

// BMP: a file header followed by an information header whose size selects the layout. The original
// twelve-byte header stores unsigned 16-bit dimensions; every later header stores signed 32-bit ones,
// with a negative height meaning top-down row order.
const readBmpContainer = (bytes: Uint8Array): ContainerReading => {
  if (!matchesSignature(bytes, BMP_SIGNATURE, 0)) {
    return { ok: false, reason: 'format-mismatch' };
  }
  if (bytes.length < BMP_MINIMUM_BYTES) {
    return { ok: false, reason: 'undecodable' };
  }
  const headerSize = readU32LE(bytes, BMP_HEADER_SIZE_OFFSET);
  const isCoreHeader = headerSize === BMP_CORE_HEADER_BYTES;
  const pixelWidth = isCoreHeader
    ? readU16LE(bytes, BMP_CORE_WIDTH_OFFSET)
    : readI32LE(bytes, BMP_INFO_WIDTH_OFFSET);
  const declaredHeight = isCoreHeader
    ? readU16LE(bytes, BMP_CORE_HEIGHT_OFFSET)
    : readI32LE(bytes, BMP_INFO_HEIGHT_OFFSET);
  const pixelHeight = Math.abs(declaredHeight);
  if (pixelWidth <= 0 || pixelHeight === 0) {
    return { ok: false, reason: 'undecodable' };
  }
  return { ok: true, measurement: { pixelWidth, pixelHeight, frameCount: 1 } };
};

// WebP: a RIFF container. An extended file states its canvas in VP8X and stores one ANMF chunk per
// animation frame; a simple file carries a single lossy VP8 or lossless VP8L bitstream whose header
// packs the dimensions into fourteen bits each. VP8X is authoritative when present, which is why the
// bitstream chunks are only consulted while no canvas has been found.
const readWebpContainer = (bytes: Uint8Array): ContainerReading => {
  if (
    !matchesSignature(bytes, RIFF_SIGNATURE, 0) ||
    readTag(bytes, WEBP_FORM_TYPE_OFFSET, 4) !== 'WEBP'
  ) {
    return { ok: false, reason: 'format-mismatch' };
  }

  let pixelWidth = 0;
  let pixelHeight = 0;
  let frameCount = 0;
  let offset = WEBP_FIRST_CHUNK_OFFSET;
  while (offset + WEBP_CHUNK_HEADER_BYTES <= bytes.length) {
    const chunkType = readTag(bytes, offset, 4);
    const chunkSize = readU32LE(bytes, offset + 4);
    const payload = offset + WEBP_CHUNK_HEADER_BYTES;
    if (payload + chunkSize > bytes.length) {
      break;
    }
    if (chunkType === 'VP8X' && chunkSize >= 10) {
      pixelWidth = readU24LE(bytes, payload + 4) + 1;
      pixelHeight = readU24LE(bytes, payload + 7) + 1;
    } else if (chunkType === 'VP8 ' && pixelWidth === 0 && chunkSize >= 10) {
      pixelWidth = readU16LE(bytes, payload + 6) & WEBP_DIMENSION_MASK;
      pixelHeight = readU16LE(bytes, payload + 8) & WEBP_DIMENSION_MASK;
    } else if (
      chunkType === 'VP8L' &&
      pixelWidth === 0 &&
      chunkSize >= 5 &&
      bytes[payload] === WEBP_VP8L_SIGNATURE
    ) {
      const packed = readU32LE(bytes, payload + 1);
      pixelWidth = (packed & WEBP_DIMENSION_MASK) + 1;
      pixelHeight = ((packed >>> 14) & WEBP_DIMENSION_MASK) + 1;
    } else if (chunkType === 'ANMF') {
      frameCount += 1;
    }
    // RIFF chunks are padded to an even length.
    offset = payload + chunkSize + (chunkSize % 2);
  }

  if (pixelWidth === 0 || pixelHeight === 0) {
    return { ok: false, reason: 'undecodable' };
  }
  return { ok: true, measurement: { pixelWidth, pixelHeight, frameCount: Math.max(frameCount, 1) } };
};

// Routes a file to the parser for its declared container. The MIME type has already been checked
// against the allow-list, so the default branch is unreachable in practice; it is present because a
// switch that cannot express "none of these" would have to lie about its return type.
const readContainer = (bytes: Uint8Array, mimeType: string): ContainerReading => {
  switch (mimeType) {
    case 'image/png':
      return readPngContainer(bytes);
    case 'image/jpeg':
      return readJpegContainer(bytes);
    case 'image/gif':
      return readGifContainer(bytes);
    case 'image/webp':
      return readWebpContainer(bytes);
    case 'image/bmp':
      return readBmpContainer(bytes);
    default:
      return { ok: false, reason: 'unsupported-type' };
  }
};

// Applies every per-image ceiling to one surface. Width and height are bounded in their own right
// because a single pixel budget cannot express a layout limit, and the decoded-byte ceiling is applied
// alongside the pixel ceiling because they are separate policy statements that happen to agree today.
const exceededCeiling = (
  pixelWidth: number,
  pixelHeight: number,
  frameCount: number,
): CellImageRejectionReason | null => {
  if (frameCount > MAX_IMAGE_FRAMES) {
    return 'too-many-frames';
  }
  if (pixelWidth > MAX_IMAGE_WIDTH || pixelHeight > MAX_IMAGE_HEIGHT) {
    return 'dimensions-too-large';
  }
  const pixels = pixelWidth * pixelHeight;
  if (pixels > MAX_IMAGE_PIXELS) {
    return 'dimensions-too-large';
  }
  if (pixels * DECODED_BYTES_PER_PIXEL > MAX_DECODED_IMAGE_BYTES) {
    return 'dimensions-too-large';
  }
  return null;
};

// Measures a file with an image element, used only where createImageBitmap is unavailable. The
// temporary object URL this needs is released on EVERY outcome — load, error and timeout alike — so the
// probe cannot pin a blob, and the timeout exists so a payload the platform never resolves cannot leave
// a drop pending forever.
const measureWithImageElement = (file: File): Promise<DecodedSurface> =>
  new Promise<DecodedSurface>((resolve) => {
    const probeUrl = URL.createObjectURL(file);
    const image = new Image();
    let settled = false;
    let timer = 0;

    const finish = (result: DecodedSurface): void => {
      if (settled) {
        return;
      }
      settled = true;
      window.clearTimeout(timer);
      image.onload = null;
      image.onerror = null;
      URL.revokeObjectURL(probeUrl);
      resolve(result);
    };

    timer = window.setTimeout(() => {
      finish({ status: 'undecodable' });
    }, DECODE_PROBE_TIMEOUT_MS);

    image.onload = (): void => {
      const width = image.naturalWidth;
      const height = image.naturalHeight;
      finish(
        width > 0 && height > 0 ? { status: 'measured', width, height } : { status: 'undecodable' },
      );
    };
    image.onerror = (): void => {
      finish({ status: 'undecodable' });
    };
    image.src = probeUrl;
  });

// Asks the platform what surface the file really decodes to, which is the figure the memory budget is
// spent on. createImageBitmap is preferred because it needs no URL and no element, and because the
// bitmap it produces can be closed the moment it has been measured — the probe therefore holds a
// decoded surface for the shortest window the platform allows.
const measureDecodedSurface = async (file: File): Promise<DecodedSurface> => {
  if (typeof createImageBitmap === 'function') {
    let bitmap: ImageBitmap;
    try {
      bitmap = await createImageBitmap(file);
    } catch {
      // A decoder that refuses the bytes is the most conclusive verdict available: whatever the label
      // and the header claimed, this file is not a displayable image of that container.
      return { status: 'undecodable' };
    }
    const width = bitmap.width;
    const height = bitmap.height;
    if (typeof bitmap.close === 'function') {
      bitmap.close();
    }
    return width > 0 && height > 0
      ? { status: 'measured', width, height }
      : { status: 'undecodable' };
  }
  if (typeof Image === 'function' && typeof URL.createObjectURL === 'function') {
    return measureWithImageElement(file);
  }
  return { status: 'unavailable' };
};

// The synchronous gate: everything that can be decided from metadata alone, in order of cost. Returns
// the reason a file is refused, or null when it is worth reading. Exported separately from the probe so
// a caller can refuse a payload without paying for a read at all.
export function inspectCellImageMetadata(file: File): CellImageRejectionReason | null {
  // Matched with some() rather than includes(): the allow-list is a readonly tuple of literal types, so
  // its own membership test would accept only those five literals while a file's type is a plain string.
  const isAcceptedType = ACCEPTED_IMAGE_MIME_TYPES.some((accepted) => accepted === file.type);
  if (!isAcceptedType) {
    return 'unsupported-type';
  }
  if (file.size === 0) {
    // No bytes cannot be an image of any container, and reading nothing would only reach the same
    // conclusion more slowly.
    return 'undecodable';
  }
  if (file.size > MAX_IMAGE_BYTES) {
    return 'too-large';
  }
  return null;
}

// The full pipeline: metadata, container signature and declared surface, per-image ceilings, a real
// decode, and the ceilings again against what the decoder reported. Resolves with the measurements an
// accepted file contributes to its entry, or with the reason it was refused. Allocates no permanent
// resource in either case, so a refusal costs nothing that has to be released later.
export async function probeCellImageFile(file: File): Promise<CellImageProbeResult> {
  // Re-checked here rather than assumed: this function is the security boundary for a file's contents,
  // and a boundary that trusts its caller to have validated the cheap things is one refactor away from
  // trusting nothing at all.
  const metadataReason = inspectCellImageMetadata(file);
  if (metadataReason !== null) {
    return { ok: false, reason: metadataReason };
  }

  const readsWholeFile = ANIMATION_CAPABLE_MIME_TYPES.some((mimeType) => mimeType === file.type);
  const region = readsWholeFile ? file : file.slice(0, IMAGE_HEADER_PROBE_BYTES);
  let bytes: Uint8Array;
  try {
    bytes = await readBlobBytes(region);
  } catch {
    return { ok: false, reason: 'undecodable' };
  }

  const reading = readContainer(bytes, file.type);
  if (!reading.ok) {
    return { ok: false, reason: reading.reason };
  }

  const declared = reading.measurement;
  // Enforced BEFORE any decode. This is what makes a header that declares an enormous canvas harmless:
  // the file is refused here, so the surface it asked for is never allocated.
  const declaredCeiling = exceededCeiling(
    declared.pixelWidth,
    declared.pixelHeight,
    declared.frameCount,
  );
  if (declaredCeiling !== null) {
    return { ok: false, reason: declaredCeiling };
  }

  const decoded = await measureDecodedSurface(file);
  if (decoded.status === 'undecodable') {
    return { ok: false, reason: 'undecodable' };
  }

  // The larger of the two measurements wins. A container may under-declare its canvas, and what has to
  // be paid for is the surface a decoder actually produced.
  let pixelWidth = declared.pixelWidth;
  let pixelHeight = declared.pixelHeight;
  if (decoded.status === 'measured') {
    pixelWidth = Math.max(pixelWidth, decoded.width);
    pixelHeight = Math.max(pixelHeight, decoded.height);
  }
  if (pixelWidth === 0 || pixelHeight === 0) {
    // Neither the container nor the platform would say how large this image is, so its decode cost
    // cannot be bounded. Refusing is the only safe answer: an unmeasured image cannot be budgeted.
    return { ok: false, reason: 'undecodable' };
  }

  const measuredCeiling = exceededCeiling(pixelWidth, pixelHeight, declared.frameCount);
  if (measuredCeiling !== null) {
    return { ok: false, reason: measuredCeiling };
  }

  return {
    ok: true,
    probe: {
      pixelWidth,
      pixelHeight,
      // One full-surface frame at the platform's cost per pixel. Animation is bounded by the frame
      // ceiling instead of multiplied in here, because a user agent's decoded-frame cache is not
      // observable from script and a guess would either over- or under-charge every animation.
      decodedBytes: pixelWidth * pixelHeight * DECODED_BYTES_PER_PIXEL,
      frameCount: declared.frameCount,
    },
  };
}
