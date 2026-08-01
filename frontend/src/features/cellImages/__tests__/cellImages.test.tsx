// The feature's primary verification vehicle: `npm run build` cannot succeed while the repository's
// pre-existing root-alias import prefix stays unresolvable, so the manual in-browser procedure is
// blocked and everything this feature claims is proven here or nowhere.
//
// components/Cell and components/Grid are deliberately not imported: they reach the store and the
// formatting helper through that same unresolvable prefix, so importing either would stop the whole
// suite from loading rather than failing one test. Local harnesses consume exactly what those
// components consume instead, through relative specifiers only.
//
// No Redux provider appears anywhere below, which makes the feature's independence from the
// persisted workbook model structural rather than asserted.
//
// jsdom implements neither object-URL API nor createImageBitmap, so all three are assigned per test:
// the exactly-one-mint and exactly-one-release assertions depend on each test owning its own call
// counts. They are installed at this file's top level rather than inside a describe block because
// Testing Library's automatic unmount runs before this file's teardown, so the provider's release
// sweep is observed while the stubs are still in place.
//
// The container fixtures are real bytes rather than mocks: acceptance depends on a container's own
// signature, declared canvas and frame count, so a fixture that only claimed to be an image would
// prove nothing about the parsers.

import { act, createEvent, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { useState } from 'react';
import { CellImageProvider, useCellImages } from '../cellImageStore';
import { useCellImageDrop } from '../useCellImageDrop';
import { CellImageOverlay } from '../CellImageOverlay';
import { probeCellImageFile } from '../cellImageValidation';
import { CELL_IMAGE_TOKENS } from '../cellImageTokens';
import {
  MAX_IMAGE_BYTES,
  MAX_IMAGE_FRAMES,
  MAX_IMAGE_PIXELS,
  MAX_RETAINED_IMAGES,
  MAX_TOTAL_IMAGE_BYTES,
  MAX_TOTAL_DECODED_BYTES,
  DECODED_BYTES_PER_PIXEL,
} from '../cellImageTokens';
import { cellImageKey } from '../cellImageKey';
import type { CellImageEntry, CellImageRejectionReason } from '../../../types/cellImage';

// Typed against the feature's own union so the assertions cannot drift from the contract: if a
// reason is ever renamed, this file stops compiling instead of silently asserting a dead string.
const UNSUPPORTED_TYPE: CellImageRejectionReason = 'unsupported-type';
const TOO_LARGE: CellImageRejectionReason = 'too-large';
const FORMAT_MISMATCH: CellImageRejectionReason = 'format-mismatch';
const UNDECODABLE: CellImageRejectionReason = 'undecodable';
const DIMENSIONS_TOO_LARGE: CellImageRejectionReason = 'dimensions-too-large';
const TOO_MANY_FRAMES: CellImageRejectionReason = 'too-many-frames';
const TOO_MANY_IMAGES: CellImageRejectionReason = 'too-many-images';
const BUDGET_EXCEEDED: CellImageRejectionReason = 'budget-exceeded';

// Deterministic and distinct per mint, which is what lets the replacement case tell the superseded
// URL from its successor.
const mintedUrl = (ordinal: number): string => `blob:cell-image-test/${ordinal}`;

let createObjectUrlSpy: jest.Mock<string, [Blob | MediaSource]>;
let revokeObjectUrlSpy: jest.Mock<void, [string]>;
let createImageBitmapSpy: jest.Mock<Promise<ImageBitmap>, [ImageBitmapSource]>;
let bitmapCloseSpy: jest.Mock<void, []>;
let originalCreateObjectURL: typeof URL.createObjectURL;
let originalRevokeObjectURL: typeof URL.revokeObjectURL;
let urlCounter = 0;

// How many times each harness cell has rendered. No identifier here contains the substring the
// testing-library lint plugin treats as naming a render utility.
const commitTally = new Map<string, number>();

const countCommit = (key: string): void => {
  commitTally.set(key, (commitTally.get(key) ?? 0) + 1);
};

const commitsFor = (key: string): number => commitTally.get(key) ?? 0;

// Captured once, before anything is installed, so that a test which needs a platform API to be ABSENT
// can put it back the way jsdom left it: undefined, but with a type the compiler accepts. This is how
// the capability-degradation branches are reached without a cast and without disabling a check.
const absentCreateObjectURL: typeof URL.createObjectURL = URL.createObjectURL;
const absentCreateImageBitmap: typeof window.createImageBitmap = window.createImageBitmap;

// The surface the stubbed decoder reports, and whether it succeeds. Assigned per test so a single
// stub can model a cooperative decoder, a decoder that disagrees with the container, and one that
// refuses the bytes outright.
let decodedProbeWidth = 1;
let decodedProbeHeight = 1;

const stubDecoder = (width: number, height: number): void => {
  decodedProbeWidth = width;
  decodedProbeHeight = height;
};

beforeEach(() => {
  urlCounter = 0;
  commitTally.clear();
  decodedProbeWidth = 1;
  decodedProbeHeight = 1;
  originalCreateObjectURL = URL.createObjectURL;
  originalRevokeObjectURL = URL.revokeObjectURL;
  // Direct assignment rather than jest.spyOn: there is no property on jsdom's URL to spy on, so
  // spyOn would throw before the first test ran.
  createObjectUrlSpy = jest.fn<string, [Blob | MediaSource]>(() => {
    urlCounter += 1;
    return mintedUrl(urlCounter);
  });
  revokeObjectUrlSpy = jest.fn<void, [string]>();
  URL.createObjectURL = createObjectUrlSpy;
  URL.revokeObjectURL = revokeObjectUrlSpy;

  // A cooperative decoder by default: it reports the surface the fixture declares and hands back a
  // bitmap whose close() the probe is expected to call, which is what lets the release of the probe's
  // own resource be asserted rather than assumed.
  bitmapCloseSpy = jest.fn<void, []>();
  createImageBitmapSpy = jest.fn<Promise<ImageBitmap>, [ImageBitmapSource]>(() =>
    Promise.resolve({
      width: decodedProbeWidth,
      height: decodedProbeHeight,
      close: bitmapCloseSpy,
    }),
  );
  window.createImageBitmap = createImageBitmapSpy;
});

afterEach(() => {
  URL.createObjectURL = originalCreateObjectURL;
  URL.revokeObjectURL = originalRevokeObjectURL;
  window.createImageBitmap = absentCreateImageBitmap;
  // Call counts must never bleed between tests: most cases assert an exact number.
  jest.clearAllMocks();
});

// --- Container fixtures -------------------------------------------------------------------------
// Real bytes, not mocks. Acceptance depends on a container's own signature, declared canvas and frame
// count, so a fixture that only pretended to be an image would prove nothing about the parsers.

const PNG_SIGNATURE = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];
const GIF89A_SIGNATURE = [0x47, 0x49, 0x46, 0x38, 0x39, 0x61];
const EMPTY_CRC = [0, 0, 0, 0];

const bigEndian32 = (value: number): number[] => [
  (value >>> 24) & 0xff,
  (value >>> 16) & 0xff,
  (value >>> 8) & 0xff,
  value & 0xff,
];

const littleEndian16 = (value: number): number[] => [value & 0xff, (value >>> 8) & 0xff];

// A PNG chunk: length, four-character type, payload, then a checksum the parsers do not verify.
const pngChunk = (type: string, data: number[]): number[] => [
  ...bigEndian32(data.length),
  ...Array.from(type).map((character) => character.charCodeAt(0)),
  ...data,
  ...EMPTY_CRC,
];

// A PNG whose header declares the given canvas. Animation frames are added as frame-control chunks,
// which is how an animated PNG states how many frames it holds.
const pngBytes = (width: number, height: number, animationFrames = 0): number[] => {
  const header = pngChunk('IHDR', [...bigEndian32(width), ...bigEndian32(height), 8, 6, 0, 0, 0]);
  const frames: number[] = [];
  for (let index = 0; index < animationFrames; index += 1) {
    frames.push(...pngChunk('fcTL', new Array<number>(26).fill(0)));
  }
  return [...PNG_SIGNATURE, ...header, ...frames, ...pngChunk('IEND', [])];
};

// A GIF whose logical screen declares the given canvas — the field that lets a handful of bytes ask a
// decoder for an enormous surface — followed by one image descriptor per frame.
const gifBytes = (width: number, height: number, frames = 1): number[] => {
  const header = [
    ...GIF89A_SIGNATURE,
    ...littleEndian16(width),
    ...littleEndian16(height),
    0x00,
    0x00,
    0x00,
  ];
  const body: number[] = [];
  for (let index = 0; index < frames; index += 1) {
    // Image separator, position and size, no local colour table, then one sub-block of pixel data.
    body.push(0x2c, 0, 0, 0, 0, 1, 0, 1, 0, 0x00, 0x02, 0x02, 0x4c, 0x01, 0x00);
  }
  return [...header, ...body, 0x3b];
};

const fileFrom = (bytes: number[], name: string, type: string): File =>
  new File([new Uint8Array(bytes)], name, { type });

const rasterFile = (name: string): File => fileFrom(pngBytes(1, 1), name, 'image/png');

// size is a read-only accessor on File, so it is redefined rather than assigned, which keeps a
// multi-mebibyte fixture free of an actual multi-mebibyte allocation.
const sizedRasterFile = (name: string, sizeBytes: number): File => {
  const file = rasterFile(name);
  Object.defineProperty(file, 'size', { value: sizeBytes });
  return file;
};

const oversizeRasterFile = (name: string): File => sizedRasterFile(name, MAX_IMAGE_BYTES + 1);

// --- Harnesses ---------------------------------------------------------------------------------

interface HarnessCellProps {
  imageKey: string;
  testId?: string;
}

// Stands in for Cell.tsx: one element carrying the drag handler set, a value node the picture
// composites over, and the overlay mounted as a sibling of that value rather than replacing it.
// The three probe nodes exist because the provider's own status region announces the human-readable
// message, while the assertions below need the machine-readable reason and the two affordance flags.
const HarnessCell = ({ imageKey, testId = 'harness-cell' }: HarnessCellProps) => {
  const { getCellImage, clearCellImage, rejection } = useCellImages();
  const { dragHandlers, isDragActive, isRejecting } = useCellImageDrop(imageKey);
  const entry = getCellImage(imageKey);

  countCommit(imageKey);

  return (
    <div data-testid={testId} className="cell" {...dragHandlers}>
      <span data-testid="cell-value">42</span>
      {entry ? (
        <CellImageOverlay entry={entry} onDismiss={() => clearCellImage(imageKey)} />
      ) : null}
      <span data-testid="drag-active">{String(isDragActive)}</span>
      <span data-testid="rejecting">{String(isRejecting)}</span>
      <span data-testid="rejection-reason">{rejection ? rejection.reason : ''}</span>
    </div>
  );
};

const renderHarness = (key: string) =>
  render(
    <CellImageProvider>
      <HarnessCell imageKey={key} />
    </CellImageProvider>,
  );

interface HarnessGridProps {
  imageKeys: string[];
}

// Several cells under one provider, which is what the aggregate budgets are measured across: a
// per-file ceiling is a property of one drop, a budget is a property of the whole page.
const HarnessGrid = ({ imageKeys }: HarnessGridProps) => (
  <>
    {imageKeys.map((imageKey) => (
      <HarnessCell key={imageKey} imageKey={imageKey} testId={`cell-${imageKey}`} />
    ))}
  </>
);

const renderGrid = (imageKeys: string[]) =>
  render(
    <CellImageProvider>
      <HarnessGrid imageKeys={imageKeys} />
    </CellImageProvider>,
  );

// The provider holds one rejection at a time, and every mounted cell mirrors its reason, so with
// more than one cell on screen the reason has to be read from a named cell rather than from the
// document. The per-cell affordance flag is what distinguishes the cell a rejection is attributed
// to from the cells that merely mirror it, so the two are always read together: a budget refusal
// must both carry the right reason AND outline the cell that was actually refused.
const rejectionOf = (cellTestId: string): { reason: string; rejecting: string } => {
  const cell = within(screen.getByTestId(cellTestId));
  return {
    reason: cell.getByTestId('rejection-reason').textContent ?? '',
    rejecting: cell.getByTestId('rejecting').textContent ?? '',
  };
};

interface LifecycleHarnessProps {
  imageKey: string;
  first: File;
  second: File;
}

// Drives the store's callbacks directly, which is the only way to put two mutations in ONE task. A
// drop cannot do that: each fireEvent is its own task, so a sequence built from drops would always be
// committed in between and would never exercise the case where ownership must be known before React
// has rendered anything.
const LifecycleHarness = ({ imageKey, first, second }: LifecycleHarnessProps) => {
  const { getCellImage, setCellImage, clearCellImage, clearAllCellImages } = useCellImages();
  const entry = getCellImage(imageKey);

  return (
    <div>
      <button
        type="button"
        data-testid="set-first"
        onClick={() => {
          setCellImage(imageKey, first);
        }}
      >
        set first
      </button>
      <button
        type="button"
        data-testid="set-then-set"
        onClick={() => {
          setCellImage(imageKey, first);
          setCellImage(imageKey, second);
        }}
      >
        set then set
      </button>
      <button
        type="button"
        data-testid="set-then-clear"
        onClick={() => {
          setCellImage(imageKey, second);
          clearCellImage(imageKey);
        }}
      >
        set then clear
      </button>
      <button
        type="button"
        data-testid="set-then-clear-all"
        onClick={() => {
          setCellImage(imageKey, second);
          clearAllCellImages();
        }}
      >
        set then clear all
      </button>
      <button
        type="button"
        data-testid="clear-twice"
        onClick={() => {
          clearCellImage(imageKey);
          clearCellImage(imageKey);
        }}
      >
        clear twice
      </button>
      <span data-testid="entry-url">{entry ? entry.objectUrl : ''}</span>
      <span data-testid="entry-name">{entry ? entry.fileName : ''}</span>
    </div>
  );
};

// A cell with no image key at all, which is how Cell.tsx renders wherever the grid supplies none.
// Both feature hooks still run, so this also proves the hook order cannot vary with the prop.
const KeylessHarnessCell = () => {
  const { getCellImage, rejection } = useCellImages();
  const { dragHandlers, isDragActive, isRejecting } = useCellImageDrop(undefined);
  const entry = getCellImage(undefined);

  return (
    <div data-testid="keyless-cell" className="cell" {...dragHandlers}>
      <span data-testid="keyless-value">42</span>
      {entry ? <span data-testid="keyless-entry">{entry.fileName}</span> : null}
      <span data-testid="keyless-drag-active">{String(isDragActive)}</span>
      <span data-testid="keyless-rejecting">{String(isRejecting)}</span>
      <span data-testid="keyless-reason">{rejection ? rejection.reason : ''}</span>
    </div>
  );
};

// The store's public surface, named without importing an unexported interface.
type CellImageStore = ReturnType<typeof useCellImages>;

interface StoreControlProps {
  label: string;
  onAct: (store: CellImageStore) => void;
}

// Runs store calls inside a single React turn, which is the exact interleaving that ownership
// tracked only during render gets wrong.
const StoreControl = ({ label, onAct }: StoreControlProps) => {
  const store = useCellImages();
  return (
    <button type="button" onClick={() => onAct(store)}>
      {label}
    </button>
  );
};

interface UnmountShellProps {
  imageKey: string;
  onAct: (store: CellImageStore) => void;
}

// Performs a store call and unmounts the provider in the same turn, so the unmount sweep and the
// call it follows cannot be verified in isolation from each other.
const UnmountShell = ({ imageKey, onAct }: UnmountShellProps) => {
  const [mounted, setMounted] = useState<boolean>(true);
  if (!mounted) {
    return null;
  }
  return (
    <CellImageProvider>
      <HarnessCell imageKey={imageKey} />
      <StoreControl
        label="act and unmount"
        onAct={(store: CellImageStore) => {
          onAct(store);
          setMounted(false);
        }}
      />
    </CellImageProvider>
  );
};

// --- Drop and settle helpers -------------------------------------------------------------------

// The plain-object shape a browser's drag data store presents to this feature: a file list plus the
// item kinds that hover-phase acceptance is decided from. A real DataTransfer cannot be constructed
// here because jsdom does not implement one.
interface TestDataTransfer {
  files: File[];
  items: Array<{ kind: string; type: string }>;
  types: string[];
  // Written by the handlers under test, which is how the advertised drop effect is observed.
  dropEffect?: string;
}

const dataTransferFor = (files: File[]): TestDataTransfer => ({
  files,
  items: files.map((file) => ({ kind: 'file', type: file.type })),
  types: ['Files'],
});

// What a drag out of another web page delivers: strings, never a File.
const stringDataTransfer = (): TestDataTransfer => ({
  files: [],
  items: [{ kind: 'string', type: 'text/uri-list' }],
  types: ['text/uri-list'],
});

const textFile = (name: string): File => new File(['plain text'], name, { type: 'text/plain' });

// jsdom rewrites some declaration values on assignment, so a token-built expectation is compared
// against itself pushed through the same normalisation, never against a literal.
const asDeclared = (property: string, value: string): string => {
  const probe = document.createElement('div');
  probe.style.setProperty(property, value);
  return probe.style.getPropertyValue(property);
};

const FOCUS_RING = `inset 0 0 0 ${CELL_IMAGE_TOKENS.dropOutlineWidth} ${CELL_IMAGE_TOKENS.statusStripColor}`;
// Shown for a hover and for a press alike, by pointer or by keyboard: red is the palette's one
// destructive signal and this control's job is to remove something.
const DESTRUCTIVE_RING = `inset 0 0 0 ${CELL_IMAGE_TOKENS.dropOutlineWidth} ${CELL_IMAGE_TOKENS.dropRejectOutlineColor}`;
const PRESSED_FILL = `inset 0 0 0 ${CELL_IMAGE_TOKENS.dismissButtonSize} ${CELL_IMAGE_TOKENS.dropActiveBackground}`;

const shadowOf = (node: HTMLElement): string => node.style.getPropertyValue('box-shadow');

const dismissControlFor = (fileName: string): HTMLElement =>
  screen.getByRole('button', { name: `Remove image ${fileName}` });

const dragFlagOf = (cellTestId: string, flagTestId: string): string =>
  within(screen.getByTestId(cellTestId)).getByTestId(flagTestId).textContent ?? '';

// Accepting a file now means reading its bytes and measuring it, both of which are asynchronous, so a
// drop settles on a later task rather than inside the event. Waiting inside act keeps every resulting
// state update inside the act environment, which is what makes the negative assertions ("nothing
// happened") trustworthy: the work has demonstrably finished rather than merely not started.
const settle = async (): Promise<void> => {
  await act(async () => {
    for (let turn = 0; turn < 4; turn += 1) {
      await new Promise<void>((resolve) => {
        window.setTimeout(resolve, 0);
      });
    }
  });
};

// dragover precedes drop, and the same data store instance carries both, because in a real browser
// the drop is never delivered unless the dragover was cancelled first.
const dropFiles = async (node: HTMLElement, files: File[]): Promise<void> => {
  const dataTransfer = dataTransferFor(files);
  fireEvent.dragOver(node, { dataTransfer });
  fireEvent.drop(node, { dataTransfer });
  await settle();
};

// Dispatches a real, cancellable native drag event on window and reports whether the provider's guard
// cancelled it. A native event is used deliberately: the guard is a native listener, not a React
// handler, and defaultPrevented is the only observable evidence that a browser would not have navigated
// away from the application.
const dispatchWindowDrag = (
  type: 'dragover' | 'drop',
  items: Array<{ kind: string; type: string }>,
): boolean => {
  const event = new Event(type, { bubbles: true, cancelable: true });
  Object.defineProperty(event, 'dataTransfer', { value: { items, files: [] } });
  window.dispatchEvent(event);
  return event.defaultPrevented;
};

const keysFor = (count: number): string[] => {
  const keys: string[] = [];
  for (let index = 0; index < count; index += 1) {
    keys.push(cellImageKey('ws-1', index, 0));
  }
  return keys;
};

describe('cell image drop', () => {
  it('renders one contained image for an accepted raster drop and mints exactly one object URL', async () => {
    renderHarness(cellImageKey('ws-1', 0, 0));

    await dropFiles(screen.getByTestId('harness-cell'), [rasterFile('photo.png')]);

    const images = screen.getAllByRole('img');
    expect(images).toHaveLength(1);
    expect(images[0]).toHaveAttribute('alt', 'photo.png');
    expect(images[0]).toHaveAttribute('src', mintedUrl(1));
    expect(createObjectUrlSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectUrlSpy).not.toHaveBeenCalled();
    // The file was measured before it was accepted, and the measurement released its own resource.
    expect(createImageBitmapSpy).toHaveBeenCalledTimes(1);
    expect(bitmapCloseSpy).toHaveBeenCalledTimes(1);
    // The picture is a layer: the cell's own value is still rendered underneath it.
    expect(screen.getByTestId('cell-value')).toHaveTextContent('42');
    // A drop consumes every outstanding drag enter, so the affordance is off again.
    expect(screen.getByTestId('drag-active')).toHaveTextContent('false');
    expect(screen.getByTestId('rejection-reason').textContent).toBe('');
  });

  it('refuses a non-image file and never mints an object URL for it', async () => {
    renderHarness(cellImageKey('ws-1', 1, 2));

    await dropFiles(screen.getByTestId('harness-cell'), [
      new File(['plain text'], 'notes.txt', { type: 'text/plain' }),
    ]);

    expect(screen.queryAllByRole('img')).toHaveLength(0);
    expect(screen.getByTestId('rejection-reason').textContent).toBe(UNSUPPORTED_TYPE);
    expect(screen.getByTestId('rejecting')).toHaveTextContent('true');
    // The load-bearing assertion of the security posture: validation runs strictly before any blob
    // URL is allocated, so a refused payload costs nothing and pins nothing.
    expect(createObjectUrlSpy).not.toHaveBeenCalled();
    expect(screen.getByRole('status')).toBeInTheDocument();
    expect(screen.getByTestId('cell-value')).toHaveTextContent('42');
  });

  it('refuses a file above the byte ceiling before any object URL is allocated', async () => {
    renderHarness(cellImageKey('ws-1', 3, 4));

    // The MIME type is one the allow-list accepts, so the drop reaches the size gate rather than
    // being turned away earlier as an unsupported type.
    await dropFiles(screen.getByTestId('harness-cell'), [oversizeRasterFile('huge.png')]);

    expect(screen.queryAllByRole('img')).toHaveLength(0);
    expect(screen.getByTestId('rejection-reason').textContent).toBe(TOO_LARGE);
    expect(screen.getByTestId('rejecting')).toHaveTextContent('true');
    expect(createObjectUrlSpy).not.toHaveBeenCalled();
    // Refused on metadata alone: the bytes were never read and the decoder was never asked.
    expect(createImageBitmapSpy).not.toHaveBeenCalled();
  });

  it('refuses a file whose contents are not the image type it claims to be', async () => {
    renderHarness(cellImageKey('ws-1', 0, 1));

    // A vector document presented as a PNG. Its declared type is on the allow-list and its size is
    // modest, so every metadata check passes; only the container signature exposes it.
    const spoofed = Array.from('<svg xmlns="http://www.w3.org/2000/svg"><script /></svg>').map(
      (character) => character.charCodeAt(0),
    );
    await dropFiles(screen.getByTestId('harness-cell'), [
      fileFrom(spoofed, 'picture.png', 'image/png'),
    ]);

    expect(screen.queryAllByRole('img')).toHaveLength(0);
    expect(screen.getByTestId('rejection-reason').textContent).toBe(FORMAT_MISMATCH);
    expect(createObjectUrlSpy).not.toHaveBeenCalled();
  });

  it('refuses a tiny file that declares an enormous canvas, without ever decoding it', async () => {
    renderHarness(cellImageKey('ws-1', 2, 2));

    // Under a hundred bytes declaring a 4096 by 4096 logical screen: the expansion that turns a
    // trivial download into tens of mebibytes of decoded memory.
    const bomb = gifBytes(4096, 4096);
    expect(bomb.length).toBeLessThan(100);
    await dropFiles(screen.getByTestId('harness-cell'), [
      fileFrom(bomb, 'innocent.gif', 'image/gif'),
    ]);

    expect(screen.queryAllByRole('img')).toHaveLength(0);
    expect(screen.getByTestId('rejection-reason').textContent).toBe(DIMENSIONS_TOO_LARGE);
    expect(createObjectUrlSpy).not.toHaveBeenCalled();
    // The decisive assertion: the ceiling is applied to the DECLARED canvas, so the surface the file
    // asked for is never allocated at all.
    expect(createImageBitmapSpy).not.toHaveBeenCalled();
  });

  it('refuses an animation that declares more frames than the ceiling allows', async () => {
    renderHarness(cellImageKey('ws-1', 2, 3));

    await dropFiles(screen.getByTestId('harness-cell'), [
      fileFrom(gifBytes(1, 1, MAX_IMAGE_FRAMES + 1), 'flipbook.gif', 'image/gif'),
    ]);

    expect(screen.queryAllByRole('img')).toHaveLength(0);
    expect(screen.getByTestId('rejection-reason').textContent).toBe(TOO_MANY_FRAMES);
    expect(createObjectUrlSpy).not.toHaveBeenCalled();
    expect(createImageBitmapSpy).not.toHaveBeenCalled();
  });

  it('refuses a file the decoder itself will not accept', async () => {
    renderHarness(cellImageKey('ws-1', 2, 4));
    createImageBitmapSpy.mockImplementation(() => Promise.reject(new Error('decode failed')));

    // A well-formed header over bytes no decoder can use: the container check passes and the decode
    // is the gate that catches it.
    await dropFiles(screen.getByTestId('harness-cell'), [rasterFile('truncated.png')]);

    expect(screen.queryAllByRole('img')).toHaveLength(0);
    expect(screen.getByTestId('rejection-reason').textContent).toBe(UNDECODABLE);
    expect(createObjectUrlSpy).not.toHaveBeenCalled();
  });

  it('refuses a file whose decoded surface exceeds the ceiling even though its header does not', async () => {
    renderHarness(cellImageKey('ws-1', 2, 5));
    // The container declares one pixel; the decoder reports a surface far past the ceiling. Trusting
    // the header alone would admit it, so the measured figure is what the ceiling is applied to.
    stubDecoder(4096, 4096);

    await dropFiles(screen.getByTestId('harness-cell'), [rasterFile('understated.png')]);

    expect(screen.queryAllByRole('img')).toHaveLength(0);
    expect(screen.getByTestId('rejection-reason').textContent).toBe(DIMENSIONS_TOO_LARGE);
    expect(createObjectUrlSpy).not.toHaveBeenCalled();
    // The probe's own bitmap is released even on the path that refuses the file.
    expect(bitmapCloseSpy).toHaveBeenCalledTimes(1);
  });

  it('dismissing a picture removes it and releases exactly the URL that was minted', async () => {
    renderHarness(cellImageKey('ws-1', 0, 0));

    await dropFiles(screen.getByTestId('harness-cell'), [rasterFile('photo.png')]);
    expect(screen.getAllByRole('img')).toHaveLength(1);

    // The removal control is a real button whose accessible name identifies the file it clears.
    fireEvent.click(screen.getByRole('button', { name: /photo\.png/ }));

    expect(screen.queryAllByRole('img')).toHaveLength(0);
    expect(revokeObjectUrlSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectUrlSpy).toHaveBeenCalledWith(mintedUrl(1));
    // Removing the picture reveals the untouched value again.
    expect(screen.getByTestId('cell-value')).toHaveTextContent('42');
  });

  it('replacing a picture on the same cell releases only the superseded URL', async () => {
    renderHarness(cellImageKey('ws-1', 2, 5));
    const node = screen.getByTestId('harness-cell');

    await dropFiles(node, [rasterFile('first.png')]);
    await dropFiles(node, [rasterFile('second.png')]);

    expect(createObjectUrlSpy).toHaveBeenCalledTimes(2);
    // One release per mint, and never the URL still on screen: the first is gone, the second lives.
    expect(revokeObjectUrlSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectUrlSpy).toHaveBeenCalledWith(mintedUrl(1));
    expect(revokeObjectUrlSpy).not.toHaveBeenCalledWith(mintedUrl(2));

    const images = screen.getAllByRole('img');
    expect(images).toHaveLength(1);
    expect(images[0]).toHaveAttribute('src', mintedUrl(2));
    expect(images[0]).toHaveAttribute('alt', 'second.png');
    // A successful drop also retires any notice still on screen.
    expect(screen.getByTestId('rejection-reason').textContent).toBe('');
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('behaves inertly and never throws when no provider is mounted above it', async () => {
    // A cell rendered outside the provider reads the context's inert default: an empty map and
    // no-op setters. That is what keeps mounting the provider from becoming a hard prerequisite.
    const key = cellImageKey(undefined, 7, 9);
    expect(key).toBe('ws:7:9');

    render(<HarnessCell imageKey={key} />);
    const node = screen.getByTestId('harness-cell');

    await expect(dropFiles(node, [rasterFile('photo.png')])).resolves.toBeUndefined();

    expect(screen.queryAllByRole('img')).toHaveLength(0);
    expect(screen.queryByRole('status')).toBeNull();
    expect(createObjectUrlSpy).not.toHaveBeenCalled();
    expect(revokeObjectUrlSpy).not.toHaveBeenCalled();
    expect(screen.getByTestId('rejection-reason').textContent).toBe('');
  });
});

describe('cell image ownership under batched mutation', () => {
  const key = cellImageKey('ws-1', 4, 4);

  const renderLifecycle = (first: File, second: File) =>
    render(
      <CellImageProvider>
        <LifecycleHarness imageKey={key} first={first} second={second} />
      </CellImageProvider>,
    );

  it('mints exactly one URL when the same cell is set twice in one task', async () => {
    renderLifecycle(rasterFile('first.png'), rasterFile('second.png'));

    // Both calls run before React has rendered either, so the second cannot learn about the first
    // from rendered state. Only the later request may commit, and the earlier one must not leave a
    // URL behind: a store that minted for both and released neither would leak here.
    fireEvent.click(screen.getByTestId('set-then-set'));
    await settle();

    expect(createObjectUrlSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectUrlSpy).not.toHaveBeenCalled();
    expect(screen.getByTestId('entry-name')).toHaveTextContent('second.png');
    expect(screen.getByTestId('entry-url')).toHaveTextContent(mintedUrl(1));
  });

  it('releases the held URL and abandons the in-flight drop when a cell is set and cleared in one task', async () => {
    renderLifecycle(rasterFile('held.png'), rasterFile('incoming.png'));

    fireEvent.click(screen.getByTestId('set-first'));
    await settle();
    expect(createObjectUrlSpy).toHaveBeenCalledTimes(1);

    // A drop and a dismissal in the same task. The dismissal is the later intent, so the held URL is
    // released exactly once and the measurement still in flight commits nothing.
    fireEvent.click(screen.getByTestId('set-then-clear'));
    await settle();

    expect(revokeObjectUrlSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectUrlSpy).toHaveBeenCalledWith(mintedUrl(1));
    expect(createObjectUrlSpy).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('entry-url').textContent).toBe('');
  });

  it('releases every held URL and abandons the in-flight drop when a cell is set and everything is cleared in one task', async () => {
    renderLifecycle(rasterFile('held.png'), rasterFile('incoming.png'));

    fireEvent.click(screen.getByTestId('set-first'));
    await settle();

    fireEvent.click(screen.getByTestId('set-then-clear-all'));
    await settle();

    expect(revokeObjectUrlSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectUrlSpy).toHaveBeenCalledWith(mintedUrl(1));
    expect(createObjectUrlSpy).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('entry-url').textContent).toBe('');
  });

  it('does not release the same URL twice when a cell is cleared twice in one task', async () => {
    renderLifecycle(rasterFile('held.png'), rasterFile('unused.png'));

    fireEvent.click(screen.getByTestId('set-first'));
    await settle();

    // The second clear sees no ownership, because the first dropped it before revoking. Reading
    // ownership from rendered state instead would still show the entry and revoke it again.
    fireEvent.click(screen.getByTestId('clear-twice'));
    await settle();

    expect(revokeObjectUrlSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectUrlSpy).toHaveBeenCalledWith(mintedUrl(1));
  });

  it('mints nothing when the provider unmounts while a drop is still being measured', async () => {
    const view = renderLifecycle(rasterFile('inflight.png'), rasterFile('unused.png'));

    fireEvent.click(screen.getByTestId('set-first'));
    // Unmounted before the measurement resolves, which is the window in which a store could mint a
    // URL that no release path would ever reach.
    view.unmount();
    await settle();

    expect(createObjectUrlSpy).not.toHaveBeenCalled();
    expect(revokeObjectUrlSpy).not.toHaveBeenCalled();
  });

  it('releases every held URL when the provider unmounts', async () => {
    const view = renderLifecycle(rasterFile('held.png'), rasterFile('unused.png'));

    fireEvent.click(screen.getByTestId('set-first'));
    await settle();
    expect(createObjectUrlSpy).toHaveBeenCalledTimes(1);

    view.unmount();

    expect(revokeObjectUrlSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectUrlSpy).toHaveBeenCalledWith(mintedUrl(1));
  });

  it('releases every held URL when every image is cleared at once', async () => {
    const keys = keysFor(3);
    renderGrid(keys);

    await dropFiles(screen.getByTestId(`cell-${keys[0]}`), [rasterFile('a.png')]);
    await dropFiles(screen.getByTestId(`cell-${keys[1]}`), [rasterFile('b.png')]);
    await dropFiles(screen.getByTestId(`cell-${keys[2]}`), [rasterFile('c.png')]);
    expect(screen.getAllByRole('img')).toHaveLength(3);

    // Every picture is dismissed individually, which walks the same release path clear-all walks and
    // proves each URL is released exactly once with no double release and nothing left behind.
    fireEvent.click(screen.getByRole('button', { name: /a\.png/ }));
    fireEvent.click(screen.getByRole('button', { name: /b\.png/ }));
    fireEvent.click(screen.getByRole('button', { name: /c\.png/ }));

    expect(screen.queryAllByRole('img')).toHaveLength(0);
    expect(createObjectUrlSpy).toHaveBeenCalledTimes(3);
    expect(revokeObjectUrlSpy).toHaveBeenCalledTimes(3);
    expect(revokeObjectUrlSpy).toHaveBeenCalledWith(mintedUrl(1));
    expect(revokeObjectUrlSpy).toHaveBeenCalledWith(mintedUrl(2));
    expect(revokeObjectUrlSpy).toHaveBeenCalledWith(mintedUrl(3));
  });
});

describe('cell image resource budgets', () => {
  it('refuses a further cell once the retained-image count budget is full, but still allows a replacement', async () => {
    const keys = keysFor(MAX_RETAINED_IMAGES + 1);
    renderGrid(keys);

    // Awaited one at a time on purpose: a budget is cumulative, so each drop has to be fully
    // committed before the next is measured against it.
    for (let index = 0; index < MAX_RETAINED_IMAGES; index += 1) {
      await dropFiles(screen.getByTestId(`cell-${keys[index]}`), [rasterFile(`held-${index}.png`)]);
    }
    expect(screen.getAllByRole('img')).toHaveLength(MAX_RETAINED_IMAGES);
    expect(createObjectUrlSpy).toHaveBeenCalledTimes(MAX_RETAINED_IMAGES);

    // One image too many. Every individual file is acceptable; the aggregate is not.
    await dropFiles(screen.getByTestId(`cell-${keys[MAX_RETAINED_IMAGES]}`), [
      rasterFile('one-too-many.png'),
    ]);

    expect(screen.getAllByRole('img')).toHaveLength(MAX_RETAINED_IMAGES);
    expect(rejectionOf(`cell-${keys[MAX_RETAINED_IMAGES]}`)).toEqual({
      reason: TOO_MANY_IMAGES,
      rejecting: 'true',
    });
    expect(createObjectUrlSpy).toHaveBeenCalledTimes(MAX_RETAINED_IMAGES);

    // A replacement is not a further image: the entry it supersedes is released by the same
    // mutation, so the budget must credit it back rather than counting both.
    await dropFiles(screen.getByTestId(`cell-${keys[0]}`), [rasterFile('replacement.png')]);

    expect(screen.getAllByRole('img')).toHaveLength(MAX_RETAINED_IMAGES);
    expect(createObjectUrlSpy).toHaveBeenCalledTimes(MAX_RETAINED_IMAGES + 1);
    expect(revokeObjectUrlSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectUrlSpy).toHaveBeenCalledWith(mintedUrl(1));
    // Accepting the replacement also retires the notice the refusal put on screen.
    expect(rejectionOf(`cell-${keys[0]}`)).toEqual({ reason: '', rejecting: 'false' });
  });

  it('frees a slot in the count budget when an image is dismissed', async () => {
    const keys = keysFor(MAX_RETAINED_IMAGES + 1);
    renderGrid(keys);

    for (let index = 0; index < MAX_RETAINED_IMAGES; index += 1) {
      await dropFiles(screen.getByTestId(`cell-${keys[index]}`), [rasterFile(`held-${index}.png`)]);
    }

    // Releasing an image releases its share of the budget in the same statement, so the next drop
    // fits where the previous one did not.
    fireEvent.click(screen.getByRole('button', { name: /held-0\.png/ }));
    await dropFiles(screen.getByTestId(`cell-${keys[MAX_RETAINED_IMAGES]}`), [
      rasterFile('now-it-fits.png'),
    ]);

    expect(screen.getAllByRole('img')).toHaveLength(MAX_RETAINED_IMAGES);
    expect(rejectionOf(`cell-${keys[MAX_RETAINED_IMAGES]}`)).toEqual({
      reason: '',
      rejecting: 'false',
    });
    expect(screen.getByAltText('now-it-fits.png')).toBeInTheDocument();
  });

  it('refuses a drop that would exceed the aggregate encoded-byte budget', async () => {
    const fileBytes = MAX_IMAGE_BYTES;
    const fittingDrops = Math.floor(MAX_TOTAL_IMAGE_BYTES / fileBytes);
    const keys = keysFor(fittingDrops + 1);
    renderGrid(keys);

    for (let index = 0; index < fittingDrops; index += 1) {
      await dropFiles(screen.getByTestId(`cell-${keys[index]}`), [
        sizedRasterFile(`large-${index}.png`, fileBytes),
      ]);
    }
    expect(screen.getAllByRole('img')).toHaveLength(fittingDrops);

    await dropFiles(screen.getByTestId(`cell-${keys[fittingDrops]}`), [
      sizedRasterFile('over-budget.png', fileBytes),
    ]);

    expect(screen.getAllByRole('img')).toHaveLength(fittingDrops);
    expect(rejectionOf(`cell-${keys[fittingDrops]}`)).toEqual({
      reason: BUDGET_EXCEEDED,
      rejecting: 'true',
    });
    expect(createObjectUrlSpy).toHaveBeenCalledTimes(fittingDrops);
  });

  it('refuses a drop that would exceed the aggregate decoded-surface budget', async () => {
    // Each image decodes to the largest surface a single image may have, so the aggregate decoded
    // budget is what runs out first: the files are tiny and the count is far below its own budget.
    const side = Math.floor(Math.sqrt(MAX_IMAGE_PIXELS));
    const decodedBytesEach = side * side * DECODED_BYTES_PER_PIXEL;
    const fittingDrops = Math.floor(MAX_TOTAL_DECODED_BYTES / decodedBytesEach);
    expect(fittingDrops).toBeLessThan(MAX_RETAINED_IMAGES);
    stubDecoder(side, side);

    const keys = keysFor(fittingDrops + 1);
    renderGrid(keys);

    for (let index = 0; index < fittingDrops; index += 1) {
      await dropFiles(screen.getByTestId(`cell-${keys[index]}`), [rasterFile(`big-${index}.png`)]);
    }
    expect(screen.getAllByRole('img')).toHaveLength(fittingDrops);

    await dropFiles(screen.getByTestId(`cell-${keys[fittingDrops]}`), [
      rasterFile('over-decoded-budget.png'),
    ]);

    expect(screen.getAllByRole('img')).toHaveLength(fittingDrops);
    expect(rejectionOf(`cell-${keys[fittingDrops]}`)).toEqual({
      reason: BUDGET_EXCEEDED,
      rejecting: 'true',
    });
    expect(createObjectUrlSpy).toHaveBeenCalledTimes(fittingDrops);
  });
});

describe('cell image window guard', () => {
  it('cancels a stray file drop anywhere in the document and leaves other drags alone', async () => {
    const view = renderHarness(cellImageKey('ws-1', 0, 0));

    // A file dropped between cells, on the ribbon, or anywhere else: without this the browser would
    // navigate away from the application to display it and end the experiment mid-assessment.
    expect(dispatchWindowDrag('dragover', [{ kind: 'file', type: 'image/png' }])).toBe(true);
    expect(dispatchWindowDrag('drop', [{ kind: 'file', type: 'image/png' }])).toBe(true);

    // A dragged link or text selection carries no file and keeps its normal browser behaviour.
    expect(dispatchWindowDrag('dragover', [{ kind: 'string', type: 'text/uri-list' }])).toBe(false);
    expect(dispatchWindowDrag('drop', [{ kind: 'string', type: 'text/html' }])).toBe(false);

    // Nothing about the guard touches a cell: no image, no rejection, no allocation.
    expect(screen.queryAllByRole('img')).toHaveLength(0);
    expect(screen.getByTestId('rejection-reason').textContent).toBe('');
    expect(createObjectUrlSpy).not.toHaveBeenCalled();

    // And it is unregistered with the provider, so it cannot outlive the feature.
    view.unmount();
    expect(dispatchWindowDrag('drop', [{ kind: 'file', type: 'image/png' }])).toBe(false);
    await settle();
  });
});

// The image element that stands in for a decoder on platforms without createImageBitmap. jsdom never
// loads a blob URL, so the outcome is announced from the constructor instead; extending the real
// constructor keeps the double a genuine image element, which is what lets it be installed without a
// cast and lets the probe's own event wiring do the work.
let probeImageOutcome: 'load' | 'error' = 'load';
let probeImageWidth = 0;
let probeImageHeight = 0;

class OutcomeImage extends Image {
  constructor() {
    super();
    Object.defineProperty(this, 'naturalWidth', { value: probeImageWidth });
    Object.defineProperty(this, 'naturalHeight', { value: probeImageHeight });
    window.setTimeout(() => {
      this.dispatchEvent(new Event(probeImageOutcome === 'load' ? 'load' : 'error'));
    }, 0);
  }
}

describe('cell image decode probe fallbacks', () => {
  const originalImage = window.Image;

  afterEach(() => {
    window.Image = originalImage;
  });

  it('measures with an image element when no bitmap decoder exists, and always releases the probe URL', async () => {
    window.createImageBitmap = absentCreateImageBitmap;
    probeImageOutcome = 'load';
    probeImageWidth = 64;
    probeImageHeight = 32;
    window.Image = OutcomeImage;

    const result = await probeCellImageFile(fileFrom(pngBytes(1, 1), 'fallback.png', 'image/png'));

    expect(result).toEqual({
      ok: true,
      probe: {
        pixelWidth: 64,
        pixelHeight: 32,
        decodedBytes: 64 * 32 * DECODED_BYTES_PER_PIXEL,
        frameCount: 1,
      },
    });
    // The probe minted a temporary URL of its own and released it: one mint, one release, and the
    // released URL is exactly the minted one.
    expect(createObjectUrlSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectUrlSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectUrlSpy).toHaveBeenCalledWith(mintedUrl(1));
  });

  it('releases the probe URL when the image element reports failure too', async () => {
    window.createImageBitmap = absentCreateImageBitmap;
    probeImageOutcome = 'error';
    probeImageWidth = 0;
    probeImageHeight = 0;
    window.Image = OutcomeImage;

    const result = await probeCellImageFile(fileFrom(pngBytes(1, 1), 'broken.png', 'image/png'));

    expect(result).toEqual({ ok: false, reason: UNDECODABLE });
    expect(revokeObjectUrlSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectUrlSpy).toHaveBeenCalledWith(mintedUrl(1));
  });

  it('falls back to the container declaration when the platform offers no way to decode at all', async () => {
    window.createImageBitmap = absentCreateImageBitmap;
    // With no way to mint a URL there is no image-element route either, which is the state of a
    // platform that supports neither. The container's own declaration is then the bound, so an
    // unmeasurable image is still never unbounded.
    URL.createObjectURL = absentCreateObjectURL;

    const accepted = await probeCellImageFile(fileFrom(pngBytes(3, 5), 'header.png', 'image/png'));
    const refused = await probeCellImageFile(
      fileFrom(pngBytes(4096, 4096), 'header-bomb.png', 'image/png'),
    );

    expect(accepted).toEqual({
      ok: true,
      probe: {
        pixelWidth: 3,
        pixelHeight: 5,
        decodedBytes: 3 * 5 * DECODED_BYTES_PER_PIXEL,
        frameCount: 1,
      },
    });
    expect(refused).toEqual({ ok: false, reason: DIMENSIONS_TOO_LARGE });
  });
});

describe('cell image editing interaction', () => {
  // A cell in edit mode suppresses the picture entirely, so the auto-focused input is never obstructed
  // by an image. Modelled here because Cell.tsx itself cannot be imported into this suite.
  const EditableHarnessCell = ({ imageKey }: HarnessCellProps) => {
    const { getCellImage, clearCellImage } = useCellImages();
    const { dragHandlers } = useCellImageDrop(imageKey);
    const [isEditing, setIsEditing] = useState(false);
    const entry = getCellImage(imageKey);

    return (
      <div data-testid="harness-cell" className="cell" {...dragHandlers}>
        <button type="button" data-testid="begin-edit" onClick={() => setIsEditing(true)}>
          edit
        </button>
        {isEditing ? <input data-testid="cell-input" defaultValue="42" /> : <span>42</span>}
        {!isEditing && entry ? (
          <CellImageOverlay entry={entry} onDismiss={() => clearCellImage(imageKey)} />
        ) : null}
      </div>
    );
  };

  it('suppresses the picture while the cell is being edited and restores it afterwards', async () => {
    render(
      <CellImageProvider>
        <EditableHarnessCell imageKey={cellImageKey('ws-1', 6, 6)} />
      </CellImageProvider>,
    );

    await dropFiles(screen.getByTestId('harness-cell'), [rasterFile('photo.png')]);
    expect(screen.getAllByRole('img')).toHaveLength(1);

    fireEvent.click(screen.getByTestId('begin-edit'));

    await waitFor(() => {
      expect(screen.getByTestId('cell-input')).toBeInTheDocument();
    });
    expect(screen.queryAllByRole('img')).toHaveLength(0);
    // Suppressing the picture is not releasing it: the URL stays valid so the picture returns intact.
    expect(revokeObjectUrlSpy).not.toHaveBeenCalled();
  });
});

describe('cell image drag affordance and drop-effect signalling', () => {
  it('cancels dragenter, dragover and drop and advertises a copy effect for a file payload', async () => {
    renderHarness(cellImageKey('ws-1', 0, 1));
    const cell = screen.getByTestId('harness-cell');

    const enterTransfer = dataTransferFor([rasterFile('photo.png')]);
    const enterEvent = createEvent.dragEnter(cell, { dataTransfer: enterTransfer });
    fireEvent(cell, enterEvent);
    expect(enterEvent.defaultPrevented).toBe(true);
    expect(enterTransfer.dropEffect).toBe('copy');

    const overTransfer = dataTransferFor([rasterFile('photo.png')]);
    const overEvent = createEvent.dragOver(cell, { dataTransfer: overTransfer });
    fireEvent(cell, overEvent);
    // Without this cancellation a browser never delivers a drop to the element at all.
    expect(overEvent.defaultPrevented).toBe(true);
    expect(overTransfer.dropEffect).toBe('copy');

    const dropEvent = createEvent.drop(cell, {
      dataTransfer: dataTransferFor([rasterFile('photo.png')]),
    });
    fireEvent(cell, dropEvent);
    // Without this cancellation the browser leaves the application to display the dropped file.
    expect(dropEvent.defaultPrevented).toBe(true);
    await settle();
  });

  it('advertises no drop effect and shows no affordance for a payload that carries no file', () => {
    renderHarness(cellImageKey('ws-1', 0, 2));
    const cell = screen.getByTestId('harness-cell');

    const transfer = stringDataTransfer();
    const overEvent = createEvent.dragOver(cell, { dataTransfer: transfer });
    fireEvent(cell, overEvent);

    expect(overEvent.defaultPrevented).toBe(true);
    expect(transfer.dropEffect).toBe('none');
    expect(screen.getByTestId('drag-active')).toHaveTextContent('false');
  });

  it('holds the affordance steady across nested enter and leave pairs, clamps at zero and resets on drop', async () => {
    renderHarness(cellImageKey('ws-1', 0, 3));
    const cell = screen.getByTestId('harness-cell');
    const transfer = () => ({ dataTransfer: dataTransferFor([rasterFile('photo.png')]) });

    fireEvent.dragEnter(cell, transfer());
    expect(screen.getByTestId('drag-active')).toHaveTextContent('true');
    // Entering a child node fires a second enter that bubbles to the same handler.
    fireEvent.dragEnter(cell, transfer());
    expect(screen.getByTestId('drag-active')).toHaveTextContent('true');
    // Leaving that child must not switch the affordance off while the pointer is still inside.
    fireEvent.dragLeave(cell, transfer());
    expect(screen.getByTestId('drag-active')).toHaveTextContent('true');
    fireEvent.dragLeave(cell, transfer());
    expect(screen.getByTestId('drag-active')).toHaveTextContent('false');

    // A leave with no matching enter must not drive the count negative and leave the affordance
    // stuck on for the rest of the session.
    fireEvent.dragLeave(cell, transfer());
    expect(screen.getByTestId('drag-active')).toHaveTextContent('false');

    // A drop consumes every outstanding enter, however many there were.
    fireEvent.dragEnter(cell, transfer());
    fireEvent.dragEnter(cell, transfer());
    fireEvent.drop(cell, transfer());
    expect(screen.getByTestId('drag-active')).toHaveTextContent('false');
    await settle();
  });

  it('takes the first acceptable image from a multi-file drop and ignores the rest', async () => {
    renderHarness(cellImageKey('ws-1', 0, 4));

    await dropFiles(screen.getByTestId('harness-cell'), [
      textFile('notes.txt'),
      rasterFile('wanted.png'),
      rasterFile('ignored.png'),
    ]);

    expect(createObjectUrlSpy).toHaveBeenCalledTimes(1);
    expect(screen.getAllByRole('img')).toHaveLength(1);
    expect(screen.getByAltText('wanted.png')).toBeInTheDocument();
    expect(screen.queryByAltText('ignored.png')).toBeNull();
    expect(screen.queryByAltText('notes.txt')).toBeNull();
  });

  it('treats a drop that carries no file as a silent no-op', async () => {
    renderHarness(cellImageKey('ws-1', 0, 5));
    const cell = screen.getByTestId('harness-cell');

    fireEvent.dragOver(cell, { dataTransfer: stringDataTransfer() });
    fireEvent.drop(cell, { dataTransfer: stringDataTransfer() });
    await settle();

    // Nothing was announced because the user did nothing wrong, and no cell was touched.
    expect(screen.queryAllByRole('img')).toHaveLength(0);
    expect(createObjectUrlSpy).not.toHaveBeenCalled();
    expect(screen.getByTestId('rejection-reason').textContent).toBe('');
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('is completely inert for a cell rendered without an image key', async () => {
    render(
      <CellImageProvider>
        <KeylessHarnessCell />
      </CellImageProvider>,
    );
    const cell = screen.getByTestId('keyless-cell');
    const transfer = dataTransferFor([rasterFile('photo.png')]);

    const enterEvent = createEvent.dragEnter(cell, { dataTransfer: transfer });
    fireEvent(cell, enterEvent);
    fireEvent.dragOver(cell, { dataTransfer: transfer });
    fireEvent.dragLeave(cell, { dataTransfer: transfer });
    fireEvent.drop(cell, { dataTransfer: dataTransferFor([rasterFile('photo.png')]) });
    await settle();

    expect(transfer.dropEffect).toBe('none');
    expect(screen.getByTestId('keyless-drag-active')).toHaveTextContent('false');
    expect(screen.queryAllByRole('img')).toHaveLength(0);
    expect(createObjectUrlSpy).not.toHaveBeenCalled();
    expect(revokeObjectUrlSpy).not.toHaveBeenCalled();
    expect(screen.queryByRole('status')).toBeNull();
  });
});

describe('cell image refusal echo and notice', () => {
  it('refuses an SVG, which the raster-only allow-list deliberately excludes', async () => {
    renderHarness(cellImageKey('ws-1', 2, 0));

    await dropFiles(screen.getByTestId('harness-cell'), [
      new File(['<svg xmlns="http://www.w3.org/2000/svg" />'], 'vector.svg', {
        type: 'image/svg+xml',
      }),
    ]);

    // The one scriptable image class is refused outright rather than sanitized, and it is refused
    // on metadata alone, so no blob URL and no decode ever happen.
    expect(screen.queryAllByRole('img')).toHaveLength(0);
    expect(screen.getByTestId('rejection-reason').textContent).toBe(UNSUPPORTED_TYPE);
    expect(createObjectUrlSpy).not.toHaveBeenCalled();
    expect(createImageBitmapSpy).not.toHaveBeenCalled();
  });

  it('refuses a scriptable format handed straight to the store, not only one that is dropped', async () => {
    render(
      <CellImageProvider>
        <HarnessCell imageKey={cellImageKey('ws-1', 2, 1)} />
        <StoreControl
          label="set svg"
          onAct={(store) => {
            store.setCellImage(
              cellImageKey('ws-1', 2, 1),
              new File(['<svg xmlns="http://www.w3.org/2000/svg" />'], 'vector.svg', {
                type: 'image/svg+xml',
              }),
            );
          }}
        />
      </CellImageProvider>,
    );

    fireEvent.click(screen.getByRole('button', { name: 'set svg' }));
    await settle();

    // The hook screens types first, so the store's own gate is only reachable this way — and it is
    // the gate that mints URLs, which is why it has to refuse the format itself.
    expect(screen.getByTestId('rejection-reason').textContent).toBe(UNSUPPORTED_TYPE);
    expect(createObjectUrlSpy).not.toHaveBeenCalled();
  });

  it('outlines only the cell whose own drop was refused', async () => {
    const keys = [cellImageKey('ws-1', 3, 0), cellImageKey('ws-1', 3, 1)];
    renderGrid(keys);

    await dropFiles(screen.getByTestId(`cell-${keys[0]}`), [textFile('notes.txt')]);

    expect(rejectionOf(`cell-${keys[0]}`)).toEqual({
      reason: UNSUPPORTED_TYPE,
      rejecting: 'true',
    });
    // The neighbour mirrors the reason, because one notice belongs to the whole page, but it is
    // not the cell that refused anything.
    expect(dragFlagOf(`cell-${keys[1]}`, 'rejecting')).toBe('false');
    expect(screen.getAllByRole('status')).toHaveLength(1);
  });

  it('retires the notice after its token lifetime and gives a later rejection a full one', () => {
    jest.useFakeTimers();
    try {
      renderHarness(cellImageKey('ws-1', 3, 2));
      const cell = screen.getByTestId('harness-cell');

      // A refusal on metadata alone is decided synchronously, before the pipeline awaits anything,
      // so the notice is on screen without waiting for a measurement that never starts.
      fireEvent.dragOver(cell, { dataTransfer: dataTransferFor([textFile('notes.txt')]) });
      fireEvent.drop(cell, { dataTransfer: dataTransferFor([textFile('notes.txt')]) });
      expect(screen.getByRole('status')).toBeInTheDocument();

      act(() => {
        jest.advanceTimersByTime(CELL_IMAGE_TOKENS.rejectionNoticeMs - 1);
      });
      expect(screen.getByRole('status')).toBeInTheDocument();

      act(() => {
        jest.advanceTimersByTime(1);
      });
      expect(screen.queryByRole('status')).toBeNull();
      expect(screen.getByTestId('rejection-reason').textContent).toBe('');
      expect(screen.getByTestId('rejecting')).toHaveTextContent('false');

      // A second refusal gets a full lifetime of its own rather than the remainder of the first.
      fireEvent.dragOver(cell, { dataTransfer: dataTransferFor([textFile('notes.txt')]) });
      fireEvent.drop(cell, { dataTransfer: dataTransferFor([textFile('notes.txt')]) });
      expect(screen.getByRole('status')).toBeInTheDocument();
      act(() => {
        jest.advanceTimersByTime(CELL_IMAGE_TOKENS.rejectionNoticeMs - 1);
      });
      expect(screen.getByRole('status')).toBeInTheDocument();
      act(() => {
        jest.advanceTimersByTime(1);
      });
      expect(screen.queryByRole('status')).toBeNull();
    } finally {
      jest.useRealTimers();
    }
  });
});

describe('cell image provider lifetime', () => {
  it('keeps a picture through a child remount and discards it only with the provider', async () => {
    const RemountHarness = () => {
      const [mounted, setMounted] = useState<boolean>(true);
      return (
        <CellImageProvider>
          <button type="button" onClick={() => setMounted((previous) => !previous)}>
            toggle cell
          </button>
          {mounted ? <HarnessCell imageKey={cellImageKey('ws-1', 4, 0)} /> : null}
        </CellImageProvider>
      );
    };
    const view = render(<RemountHarness />);

    await dropFiles(screen.getByTestId('harness-cell'), [rasterFile('photo.png')]);
    expect(screen.getAllByRole('img')).toHaveLength(1);

    const toggle = screen.getByRole('button', { name: 'toggle cell' });
    fireEvent.click(toggle);
    expect(screen.queryByTestId('harness-cell')).toBeNull();
    fireEvent.click(toggle);

    // The map lives above the child, so a remount finds the same picture and the same blob URL.
    expect(screen.getAllByRole('img')).toHaveLength(1);
    expect(screen.getByAltText('photo.png')).toHaveAttribute('src', mintedUrl(1));
    expect(createObjectUrlSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectUrlSpy).not.toHaveBeenCalled();

    view.unmount();
    expect(revokeObjectUrlSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectUrlSpy).toHaveBeenCalledWith(mintedUrl(1));
  });

  it('subscribes only to the two drag events on window and never intercepts a key press', () => {
    const addEventListenerSpy = jest.spyOn(window, 'addEventListener');
    renderHarness(cellImageKey('ws-1', 4, 1));

    const subscribed = addEventListenerSpy.mock.calls.map((call) => String(call[0]));
    expect(subscribed).toContain('dragover');
    expect(subscribed).toContain('drop');
    // The provider adds no keyboard listeners, preserving Grid's existing key handling.
    expect(subscribed).not.toContain('keydown');
    expect(subscribed).not.toContain('keyup');
    addEventListenerSpy.mockRestore();

    const keyEvent = new Event('keydown', { bubbles: true, cancelable: true });
    window.dispatchEvent(keyEvent);
    expect(keyEvent.defaultPrevented).toBe(false);
  });
});

describe('cell image release is idempotent within one React turn', () => {
  it('releases once, not twice, when one turn clears a cell and unmounts the provider', async () => {
    const key = cellImageKey('ws-1', 5, 0);
    render(<UnmountShell imageKey={key} onAct={(store) => store.clearCellImage(key)} />);

    await dropFiles(screen.getByTestId('harness-cell'), [rasterFile('photo.png')]);
    expect(createObjectUrlSpy).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: 'act and unmount' }));

    // The URL leaves the live set before it is revoked, so the unmount sweep that follows in the
    // same turn finds nothing left to release.
    expect(revokeObjectUrlSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectUrlSpy).toHaveBeenCalledWith(mintedUrl(1));
    await settle();
  });

  it('mints nothing further and releases the held URL once when one turn replaces a picture and unmounts', async () => {
    const key = cellImageKey('ws-1', 5, 1);
    render(
      <UnmountShell
        imageKey={key}
        onAct={(store) => store.setCellImage(key, rasterFile('second.png'))}
      />,
    );

    await dropFiles(screen.getByTestId('harness-cell'), [rasterFile('first.png')]);
    expect(createObjectUrlSpy).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: 'act and unmount' }));
    await settle();

    // The replacement is abandoned rather than committed: its measurement resolves after the
    // provider is gone, so it mints nothing that no release path remains to free. The picture that
    // was held is released exactly once, by the sweep.
    expect(createObjectUrlSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectUrlSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectUrlSpy).toHaveBeenCalledWith(mintedUrl(1));
  });
});

describe('cell image render scoping', () => {
  it('broadcasts to every mounted cell on a real change but not on a no-op', async () => {
    const keys = [cellImageKey('ws-1', 6, 0), cellImageKey('ws-1', 6, 1)];
    const absentKey = cellImageKey('ws-1', 9, 9);
    render(
      <CellImageProvider>
        <HarnessCell imageKey={keys[0]} testId={`cell-${keys[0]}`} />
        <HarnessCell imageKey={keys[1]} testId={`cell-${keys[1]}`} />
        <StoreControl label="clear absent" onAct={(store) => store.clearCellImage(absentKey)} />
      </CellImageProvider>,
    );

    const beforeDrop = commitsFor(keys[1]);
    await dropFiles(screen.getByTestId(`cell-${keys[0]}`), [rasterFile('photo.png')]);

    // Measured, not assumed: one cell's picture re-renders its untouched neighbour too, because the
    // context carries the whole map. That is the accepted prototype limitation the store documents.
    expect(commitsFor(keys[1])).toBeGreaterThan(beforeDrop);

    const beforeNoop = commitsFor(keys[1]);
    fireEvent.click(screen.getByRole('button', { name: 'clear absent' }));

    // Clearing a key that holds nothing returns the identical state, so the broadcast never happens
    // at all: the reducer's guards keep needless work out of the grid.
    expect(commitsFor(keys[1])).toBe(beforeNoop);
    expect(commitsFor(keys[0])).toBeGreaterThan(0);
  });
});

describe('cell image overlay layer and removal control', () => {
  it('clips the picture inside an inert layer that fills the whole cell box', () => {
    // Rendered in isolation so the layer is the single generic element under the host. Reaching it
    // by role keeps the assertion free of container access and node traversal, both of which this
    // project's lint configuration rejects.
    const entry: CellImageEntry = {
      objectUrl: 'blob:cell-image-layer',
      fileName: 'photo.png',
      mimeType: 'image/png',
      sizeBytes: 2048,
      pixelWidth: 8,
      pixelHeight: 8,
      decodedBytes: 8 * 8 * DECODED_BYTES_PER_PIXEL,
      frameCount: 1,
      droppedAt: 0,
    };
    render(
      <div data-testid="layer-host">
        <CellImageOverlay entry={entry} onDismiss={() => undefined} />
      </div>,
    );

    const layer = within(screen.getByTestId('layer-host')).getByRole('generic');

    // Absolute with every inset at zero is what confines the picture to the cell's own box. Those
    // insets resolve against the cell only because the cell declares a positioned containing block.
    expect(layer.style.position).toBe('absolute');
    expect(layer.style.top).toBe('0px');
    expect(layer.style.right).toBe('0px');
    expect(layer.style.bottom).toBe('0px');
    expect(layer.style.left).toBe('0px');
    // Clipped, never grown: a picture bigger than its cell cannot push out row height or column width.
    expect(layer.style.overflow).toBe('hidden');
    expect(layer.style.zIndex).toBe(String(CELL_IMAGE_TOKENS.overlayZIndex));
    // Transparent to pointer input, so a click on the picture still reaches the cell root and
    // click-to-edit survives. Only the removal control opts back in.
    expect(layer.style.pointerEvents).toBe('none');
  });

  it('scales the picture inside the cell box instead of resizing the cell', async () => {
    renderHarness(cellImageKey('ws-1', 7, 0));
    await dropFiles(screen.getByTestId('harness-cell'), [rasterFile('photo.png')]);

    const image = screen.getByRole('img');
    expect(image.style.maxWidth).toBe('100%');
    expect(image.style.maxHeight).toBe('100%');
    expect(image.style.getPropertyValue('object-fit')).toBe('contain');
    expect(image.style.display).toBe('block');
  });

  it('exposes the picture and a real labelled button that stays clickable inside the inert layer', async () => {
    renderHarness(cellImageKey('ws-1', 7, 1));
    await dropFiles(screen.getByTestId('harness-cell'), [rasterFile('holiday photo.png')]);

    expect(screen.getByRole('img')).toHaveAttribute('alt', 'holiday photo.png');
    const control = dismissControlFor('holiday photo.png');
    expect(control).toHaveAttribute('type', 'button');
    expect(control).toHaveAccessibleName('Remove image holiday photo.png');
    expect(control.style.pointerEvents).toBe('auto');
  });

  it('draws its own hover, pressed and focus treatment as an unclippable inset ring', async () => {
    renderHarness(cellImageKey('ws-1', 7, 2));
    await dropFiles(screen.getByTestId('harness-cell'), [rasterFile('photo.png')]);
    const control = dismissControlFor('photo.png');

    expect(shadowOf(control)).toBe(asDeclared('box-shadow', 'none'));
    // The user agent ring is suppressed only because an inset ring replaces it: an outline is
    // painted outside the border box, where the layer's clip would eat it.
    expect(control.style.outlineStyle).toBe('none');

    fireEvent.mouseEnter(control);
    expect(shadowOf(control)).toBe(asDeclared('box-shadow', DESTRUCTIVE_RING));

    fireEvent.mouseDown(control);
    // The ring is listed first so it paints on top of the fill rather than under it.
    expect(shadowOf(control)).toBe(
      asDeclared('box-shadow', `${DESTRUCTIVE_RING}, ${PRESSED_FILL}`),
    );

    fireEvent.mouseUp(control);
    fireEvent.mouseLeave(control);
    expect(shadowOf(control)).toBe(asDeclared('box-shadow', 'none'));

    // Focus outranks pointer state so a keyboard user can always see where they are.
    fireEvent.focus(control);
    expect(shadowOf(control)).toBe(asDeclared('box-shadow', FOCUS_RING));
    fireEvent.mouseEnter(control);
    expect(shadowOf(control)).toBe(asDeclared('box-shadow', FOCUS_RING));

    fireEvent.blur(control);
    fireEvent.mouseLeave(control);
    expect(shadowOf(control)).toBe(asDeclared('box-shadow', 'none'));
  });

  it('does not let a keyboard press on the control remove the picture', async () => {
    renderHarness(cellImageKey('ws-1', 7, 3));
    await dropFiles(screen.getByTestId('harness-cell'), [rasterFile('photo.png')]);
    const control = dismissControlFor('photo.png');

    // A keyboard press reads exactly like a pointer press, ring and fill together, so the two input
    // paths give the same feedback.
    fireEvent.keyDown(control, { key: 'Enter' });
    expect(shadowOf(control)).toBe(
      asDeclared('box-shadow', `${DESTRUCTIVE_RING}, ${PRESSED_FILL}`),
    );

    fireEvent.keyUp(control, { key: 'Enter' });
    expect(shadowOf(control)).toBe(asDeclared('box-shadow', 'none'));
    // Still there: a key press is feedback, and only an activation removes the picture.
    expect(screen.getByRole('img')).toBeInTheDocument();
    expect(revokeObjectUrlSpy).not.toHaveBeenCalled();
  });
});

describe('cell image addressing', () => {
  it('derives a distinct key for every worksheet, row and column', () => {
    expect(cellImageKey('ws-1', 0, 0)).toBe('ws-1:0:0');
    expect(cellImageKey('ws-1', 1, 4)).toBe('ws-1:1:4');
    expect(cellImageKey('ws-2', 1, 4)).toBe('ws-2:1:4');
    // The grid reads its worksheet id from a value the compiler cannot narrow, so the helper has to
    // tolerate its absence rather than force a cast at the render site.
    expect(cellImageKey(undefined, 3, 7)).toBe('ws:3:7');
  });

  it('keeps a picture addressed by its own key while a neighbour stays empty', async () => {
    const keys = [cellImageKey('ws-1', 8, 0), cellImageKey('ws-1', 8, 1)];
    renderGrid(keys);

    await dropFiles(screen.getByTestId(`cell-${keys[1]}`), [rasterFile('neighbour.png')]);

    const images = screen.getAllByRole('img');
    expect(images).toHaveLength(1);
    expect(images[0]).toHaveAttribute('alt', 'neighbour.png');
    expect(within(screen.getByTestId(`cell-${keys[1]}`)).getByRole('img')).toBeInTheDocument();
    expect(keys[0]).not.toBe(keys[1]);
  });
});
