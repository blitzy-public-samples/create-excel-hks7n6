// Keep image blobs outside the workbook/Redux model so they remain page-scoped
// and never enter persisted payloads.
// This provider owns each object URL until replacement, explicit clear,
// clear-all, or unmount; URLs stay valid across React renders, so they are not
// revoked on image load.
// Ownership lives in a ref that is written synchronously, never derived from
// rendered state: React commits state asynchronously, so a record read from a
// render would be stale for the same-task sequences that orphan a URL or revoke
// one twice.
// A measurement that resolves after its request stopped being current is
// abandoned, so a superseded, cleared or unmounted request mints nothing.
// What is retained is budgeted as well as what is accepted, and the accounting is
// derived from that same ownership ref so it cannot drift from what is held.
// A declared type and a compressed length are payload-controlled, so
// cellImageValidation verifies the container's own bytes before anything is
// minted.
// RENDER SCOPING is an accepted, measured limitation: the context value carries
// the whole map and the single pending rejection, so a real change re-renders
// every mounted cell rather than only the one that changed. Key-scoped
// subscriptions would have to add an export and drop those two members, changing
// a contract this experiment fixed, so what is done instead is to remove every
// AVOIDABLE broadcast — see the identity-preserving guards in the reducer.
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useReducer,
  useRef,
} from 'react';
import type { CSSProperties, ReactNode } from 'react';
import type {
  CellImageEntry,
  CellImageKey,
  CellImageMap,
  CellImageRejection,
  CellImageRejectionReason,
} from '../../types/cellImage';
import type { CellImageProbeResult } from './cellImageValidation';
import { inspectCellImageMetadata, probeCellImageFile } from './cellImageValidation';
import {
  ACCEPTED_IMAGE_MIME_TYPES,
  CELL_IMAGE_TOKENS,
  MAX_IMAGE_BYTES,
  MAX_IMAGE_FRAMES,
  MAX_IMAGE_HEIGHT,
  MAX_IMAGE_PIXELS,
  MAX_IMAGE_WIDTH,
  MAX_RETAINED_IMAGES,
  MAX_TOTAL_DECODED_BYTES,
  MAX_TOTAL_IMAGE_BYTES,
} from './cellImageTokens';

// Human-readable format list for the rejection notice, derived from the
// allow-list so the wording can never drift from what validation accepts.
const ACCEPTED_IMAGE_LABELS = ACCEPTED_IMAGE_MIME_TYPES.map((mimeType) =>
  mimeType.slice(mimeType.indexOf('/') + 1).toUpperCase(),
).join(', ');

// Every figure the notice quotes is derived from the token that enforces it, so
// a limit and its explanation cannot disagree.
const BYTES_PER_MEBIBYTE = 1024 * 1024;
const MAX_IMAGE_MEBIBYTES = MAX_IMAGE_BYTES / BYTES_PER_MEBIBYTE;
const MAX_IMAGE_MEGAPIXELS = MAX_IMAGE_PIXELS / BYTES_PER_MEBIBYTE;
const MAX_TOTAL_IMAGE_MEBIBYTES = MAX_TOTAL_IMAGE_BYTES / BYTES_PER_MEBIBYTE;
const MAX_TOTAL_DECODED_MEBIBYTES = MAX_TOTAL_DECODED_BYTES / BYTES_PER_MEBIBYTE;

// Keep the notice outside layout and non-interactive so it cannot shift or block
// the grid. Logical insets preserve placement in RTL layouts.
const statusStripStyle: CSSProperties = {
  position: 'fixed',
  insetInlineStart: 0,
  insetInlineEnd: 0,
  insetBlockEnd: 0,
  insetBlockStart: 'auto',
  zIndex: CELL_IMAGE_TOKENS.overlayZIndex,
  backgroundColor: CELL_IMAGE_TOKENS.statusStripBackground,
  color: CELL_IMAGE_TOKENS.statusStripColor,
  overflowWrap: 'break-word',
  pointerEvents: 'none',
};

interface CellImageState {
  images: CellImageMap;
  rejection: CellImageRejection | null;
}

type CellImageAction =
  | { type: 'set'; key: CellImageKey; entry: CellImageEntry }
  | { type: 'clear'; key: CellImageKey }
  | { type: 'clearAll' }
  | { type: 'reject'; rejection: CellImageRejection }
  | { type: 'dismiss-rejection' };

// Holding the URL and its two costs together is what lets one statement release a
// URL and its share of the budget, so the two can never disagree.
interface OwnedCellImage {
  objectUrl: string;
  sizeBytes: number;
  decodedBytes: number;
}

interface RetainedTotals {
  count: number;
  encodedBytes: number;
  decodedBytes: number;
}

// Key-based operations accept undefined so cells without an imageKey remain
// inert without caller-side guards.
interface CellImageContextValue {
  images: CellImageMap;
  rejection: CellImageRejection | null;
  getCellImage: (key: string | undefined) => CellImageEntry | undefined;
  setCellImage: (key: string | undefined, file: File) => void;
  rejectCellImage: (
    key: string | undefined,
    reason: CellImageRejectionReason,
    fileName: string,
  ) => void;
  clearCellImage: (key: string | undefined) => void;
  clearAllCellImages: () => void;
  dismissRejection: () => void;
}

interface CellImageProviderProps {
  children: ReactNode;
}

// Keep URL, timestamp, timer, and event side effects outside this reducer. A branch
// that changes nothing returns the received state untouched, identity included, so a
// transition with no effect costs no render anywhere in the grid.
function cellImageReducer(state: CellImageState, action: CellImageAction): CellImageState {
  switch (action.type) {
    case 'set':
      // A successful drop also clears any notice still on screen: the complaint
      // the user just acted on should not outlive the correction.
      return { images: { ...state.images, [action.key]: action.entry }, rejection: null };
    case 'clear': {
      // Clearing a cell that holds no image must not manufacture a new state
      // object: the context value would change identity and every mounted cell
      // would re-render for nothing.
      if (!(action.key in state.images)) {
        return state;
      }
      const next: CellImageMap = { ...state.images };
      delete next[action.key];
      return { images: next, rejection: state.rejection };
    }
    case 'clearAll':
      // Same guard: an already-empty map is left as it is, identity included.
      if (Object.keys(state.images).length === 0) {
        return state;
      }
      return { images: {}, rejection: state.rejection };
    case 'reject':
      return { images: state.images, rejection: action.rejection };
    case 'dismiss-rejection':
      // Same guard: the auto-dismiss timer and an explicit dismissal can both
      // arrive with no notice on screen.
      if (state.rejection === null) {
        return state;
      }
      return { images: state.images, rejection: null };
    default:
      return state;
  }
}

// Kept separate from buildRejection so that adding a reason to the union forces a
// matching phrase: the switch covers the whole union, so an unhandled member
// stops the file compiling rather than shipping a notice that says nothing.
function rejectionDetail(reason: CellImageRejectionReason): string {
  switch (reason) {
    case 'unsupported-type':
      return `only ${ACCEPTED_IMAGE_LABELS} images can be dropped into a cell`;
    case 'too-large':
      return `an image file must be under ${MAX_IMAGE_MEBIBYTES} MiB`;
    case 'format-mismatch':
      return 'its contents are not the image type its name claims';
    case 'undecodable':
      return 'it could not be read as an image';
    case 'dimensions-too-large':
      return `an image must be at most ${MAX_IMAGE_WIDTH} by ${MAX_IMAGE_HEIGHT} pixels and ${MAX_IMAGE_MEGAPIXELS} megapixels`;
    case 'too-many-frames':
      return `an animated image must have at most ${MAX_IMAGE_FRAMES} frames`;
    case 'too-many-images':
      return `at most ${MAX_RETAINED_IMAGES} images can be held at once, so remove one first`;
    case 'budget-exceeded':
      return `the images already held use the ${MAX_TOTAL_IMAGE_MEBIBYTES} MiB of files and ${MAX_TOTAL_DECODED_MEBIBYTES} MiB of decoded memory this experiment budgets, so remove one first`;
    default:
      return 'it could not be accepted';
  }
}

function buildRejection(
  key: CellImageKey,
  reason: CellImageRejectionReason,
  fileName: string,
): CellImageRejection {
  // A dragged payload can legitimately carry an empty name, so fall back to a
  // neutral label rather than announcing a blank.
  const label = fileName.length > 0 ? fileName : 'the dropped file';
  return { key, reason, fileName, message: `Skipped ${label}: ${rejectionDetail(reason)}.` };
}

// One shared empty map keeps the inert default's identity stable, so a consumer
// rendered without a provider never sees a changing context value. Frozen
// because it is shared: the reducer never mutates a map, and this makes an
// accidental write a visible error rather than a silent cross-instance leak.
const EMPTY_CELL_IMAGE_MAP: CellImageMap = Object.freeze({});

const INITIAL_CELL_IMAGE_STATE: CellImageState = {
  images: EMPTY_CELL_IMAGE_MAP,
  rejection: null,
};

// The default is a working inert implementation rather than undefined, so a cell
// renders identically whether or not a provider is mounted above it and mounting
// the provider never becomes a prerequisite for an existing page or test.
const NO_OP_CELL_IMAGE_CONTEXT: CellImageContextValue = Object.freeze({
  images: EMPTY_CELL_IMAGE_MAP,
  rejection: null,
  getCellImage: () => undefined,
  setCellImage: () => undefined,
  rejectCellImage: () => undefined,
  clearCellImage: () => undefined,
  clearAllCellImages: () => undefined,
  dismissRejection: () => undefined,
});

const CellImageContext = createContext<CellImageContextValue>(NO_OP_CELL_IMAGE_CONTEXT);

export function CellImageProvider({ children }: CellImageProviderProps): JSX.Element {
  const [state, dispatch] = useReducer(cellImageReducer, INITIAL_CELL_IMAGE_STATE);

  // The canonical ownership registry: written synchronously wherever a URL is
  // minted or released, so every release path sees what is owned at the instant it
  // runs rather than at the last commit. It is also the sole basis of the budgets.
  const ownedRef = useRef<Map<CellImageKey, OwnedCellImage>>(new Map());

  // True once this provider has gone away. A measurement that resolves
  // afterwards must not mint a URL, because no release path remains to free it.
  const disposedRef = useRef<boolean>(false);

  // The claim a pending measurement holds on its cell key. Every mutation of a
  // key issues a new claim and every clear drops it, so a measurement can tell
  // whether it is still the current intent for that cell before it commits.
  // Monotonic ids rather than a boolean: two measurements for the same key must
  // be distinguishable, and only the latest may win.
  const claimCounterRef = useRef<number>(0);
  const claimsRef = useRef<Map<CellImageKey, number>>(new Map());

  // Every object URL minted and not yet released. Membership is what makes release
  // idempotent and what tells the unmount sweep which URLs are still outstanding.
  const liveUrlsRef = useRef<Set<string>>(new Set());

  // The single release site for the whole store. Deleting from the live set BEFORE
  // revoking is the load-bearing detail: a second release of the same URL — a
  // dismissal followed by an unmount in one turn, for instance — finds nothing to
  // delete and returns without revoking, so no URL is ever revoked twice.
  const releaseObjectUrl = useCallback((objectUrl: string): void => {
    if (!liveUrlsRef.current.delete(objectUrl)) {
      return;
    }
    URL.revokeObjectURL(objectUrl);
  }, []);

  const getCellImage = useCallback(
    (key: string | undefined): CellImageEntry | undefined => {
      if (key === undefined) {
        return undefined;
      }
      // Indexed access is not checked by this project's compiler settings, so
      // presence is tested explicitly instead of trusting the read.
      return key in state.images ? state.images[key] : undefined;
    },
    [state.images],
  );

  // Sums what is retained right now, excluding the key that is about to be
  // written. That exclusion is the replacement credit: an image being replaced
  // is released as part of the same mutation, so charging for it as well would
  // make a replacement cost twice what it actually holds.
  const measureRetained = useCallback((excludedKey: CellImageKey): RetainedTotals => {
    let count = 0;
    let encodedBytes = 0;
    let decodedBytes = 0;
    ownedRef.current.forEach((owned, key) => {
      if (key === excludedKey) {
        return;
      }
      count += 1;
      encodedBytes += owned.sizeBytes;
      decodedBytes += owned.decodedBytes;
    });
    return { count, encodedBytes, decodedBytes };
  }, []);

  // The aggregate budgets, judged against the registry. Returns the reason the
  // image cannot be retained, or null when it fits.
  const budgetRejection = useCallback(
    (key: CellImageKey, sizeBytes: number, decodedBytes: number): CellImageRejectionReason | null => {
      const retained = measureRetained(key);
      if (retained.count + 1 > MAX_RETAINED_IMAGES) {
        return 'too-many-images';
      }
      if (retained.encodedBytes + sizeBytes > MAX_TOTAL_IMAGE_BYTES) {
        return 'budget-exceeded';
      }
      if (retained.decodedBytes + decodedBytes > MAX_TOTAL_DECODED_BYTES) {
        return 'budget-exceeded';
      }
      return null;
    },
    [measureRetained],
  );

  // Accepts a file for one cell, or refuses it with a reason. Validation and
  // budgeting both complete before an object URL exists, so a refused payload
  // allocates nothing at all.
  const setCellImage = useCallback(
    async (key: string | undefined, file: File): Promise<void> => {
      if (key === undefined) {
        return;
      }

      // Metadata gate: the allow-list, an empty file and the per-file ceiling,
      // none of which needs the file to be read.
      const metadataReason = inspectCellImageMetadata(file);
      if (metadataReason !== null) {
        dispatch({ type: 'reject', rejection: buildRejection(key, metadataReason, file.name) });
        return;
      }

      // Budget preflight: refuse before reading or decoding anything if this
      // image could not be retained even were it valid. The decoded cost is not
      // known yet, so only the count and encoded budgets can be judged here;
      // both are judged again below against the measured figure.
      const preflightReason = budgetRejection(key, file.size, 0);
      if (preflightReason !== null) {
        dispatch({ type: 'reject', rejection: buildRejection(key, preflightReason, file.name) });
        return;
      }

      // Claim the key for this request. Another drop on the same key issues a
      // newer claim, and a clear or a clear-all drops the claim outright, so the
      // check after the measurement can tell whether this request is still what
      // the user asked for.
      claimCounterRef.current += 1;
      const claim = claimCounterRef.current;
      claimsRef.current.set(key, claim);

      let validated: CellImageProbeResult;
      try {
        validated = await probeCellImageFile(file);
      } catch {
        // The probe handles its own read and decode failures, so reaching here
        // means the platform failed in a way it does not model. The honest
        // report is the same one a decoder refusal gets, and it leaves the cell
        // untouched.
        validated = { ok: false, reason: 'undecodable' };
      }

      const holdsClaim = claimsRef.current.get(key) === claim;
      if (disposedRef.current || !holdsClaim) {
        // Superseded, cleared, or unmounted while the file was being measured.
        // Nothing has been minted yet, so abandoning the commit releases nothing
        // and leaks nothing; the mutation that invalidated this claim owns
        // whatever was held before it.
        return;
      }
      claimsRef.current.delete(key);

      if (!validated.ok) {
        dispatch({ type: 'reject', rejection: buildRejection(key, validated.reason, file.name) });
        return;
      }

      // The budgets again, now that the decoded cost is known, immediately
      // before the mint. Nothing is awaited between this check and the registry
      // write, so no other commit can interleave and let both overspend.
      const budgetReason = budgetRejection(key, file.size, validated.probe.decodedBytes);
      if (budgetReason !== null) {
        dispatch({ type: 'reject', rejection: buildRejection(key, budgetReason, file.name) });
        return;
      }

      const superseded = ownedRef.current.get(key);
      const objectUrl = URL.createObjectURL(file);
      liveUrlsRef.current.add(objectUrl);
      // Ownership is recorded BEFORE the dispatch that renders it, and this same
      // assignment removes the superseded URL from the registry, so the URL
      // about to be revoked is already unreachable and its budget is already
      // released.
      ownedRef.current.set(key, {
        objectUrl,
        sizeBytes: file.size,
        decodedBytes: validated.probe.decodedBytes,
      });

      dispatch({
        type: 'set',
        key,
        entry: {
          objectUrl,
          fileName: file.name,
          mimeType: file.type,
          sizeBytes: file.size,
          pixelWidth: validated.probe.pixelWidth,
          pixelHeight: validated.probe.pixelHeight,
          decodedBytes: validated.probe.decodedBytes,
          frameCount: validated.probe.frameCount,
          droppedAt: Date.now(),
        },
      });

      // Release path (a): the superseded URL is released exactly once, after the
      // replacement is in flight, so no render ever points at a revoked blob.
      if (superseded !== undefined) {
        releaseObjectUrl(superseded.objectUrl);
      }
    },
    [budgetRejection, releaseObjectUrl],
  );

  // Lets the drop hook surface a payload it rejected before this store ever saw
  // a File — a drag that carried files but none of an accepted type, for
  // instance. Allocates nothing and releases nothing.
  const rejectCellImage = useCallback(
    (key: string | undefined, reason: CellImageRejectionReason, fileName: string): void => {
      if (key === undefined) {
        return;
      }
      dispatch({ type: 'reject', rejection: buildRejection(key, reason, fileName) });
    },
    [],
  );

  const clearCellImage = useCallback(
    (key: string | undefined): void => {
      if (key === undefined) {
        return;
      }
      // A clear is the later intent, so a measurement still in flight for this key
      // loses its claim: without this, an image the user has already dismissed
      // could reappear when its measurement finished.
      claimsRef.current.delete(key);
      const owned = ownedRef.current.get(key);
      // Ownership is dropped before the URL is released, so a released URL is never
      // reachable from the registry and a repeated clear cannot revoke it twice.
      // The same statement releases this image's share of the budget.
      ownedRef.current.delete(key);
      dispatch({ type: 'clear', key });
      // Release path (b): the explicit dismiss. The cell's own value and formula
      // are untouched, so removing the image simply reveals them again.
      if (owned !== undefined) {
        releaseObjectUrl(owned.objectUrl);
      }
    },
    [releaseObjectUrl],
  );

  const clearAllCellImages = useCallback((): void => {
    // Every in-flight measurement loses its claim, because a clear-all is an
    // explicit "hold nothing", and a drop that lands afterwards would contradict
    // it.
    claimsRef.current.clear();
    // Snapshot, then empty the registry, then revoke: after the clear no
    // released URL is reachable, and the whole budget is released with it.
    const released = Array.from(ownedRef.current.values());
    ownedRef.current.clear();
    dispatch({ type: 'clearAll' });
    // Release path (c): every URL the registry held, each released exactly once.
    released.forEach((owned) => {
      releaseObjectUrl(owned.objectUrl);
    });
  }, [releaseObjectUrl]);

  const dismissRejection = useCallback((): void => {
    dispatch({ type: 'dismiss-rejection' });
  }, []);

  // Release path (d): the unmount sweep. The empty dependency list is required
  // so the cleanup runs only when the provider goes away.
  useEffect(() => {
    // Both registries are created once and only ever mutated in place, so binding
    // their identities here still observes everything held at the moment the
    // cleanup RUNS, including a URL minted since the last commit.
    const owned = ownedRef.current;
    const claims = claimsRef.current;
    const live = liveUrlsRef.current;
    // Reset on mount so a provider that is mounted, unmounted and mounted again
    // comes back alive rather than staying disposed.
    disposedRef.current = false;
    return () => {
      // Marked disposed first, so a measurement resolving after this point
      // abandons its commit instead of minting a URL with no owner left to
      // release it.
      disposedRef.current = true;
      claims.clear();
      // The live set is swept rather than the ownership map, so the sweep releases
      // exactly what is still outstanding — never a URL an earlier dismissal or
      // replacement in the same turn already released.
      Array.from(live).forEach(releaseObjectUrl);
      owned.clear();
    };
  }, [releaseObjectUrl]);

  // Guard against a stray file drop. A browser handles a dropped file by default
  // — opening or downloading it — even when the drop lands outside a registered
  // target, so a drop that misses a cell by a few pixels would navigate away from
  // the application and end the experiment mid-assessment. Cancelling the default
  // at window level prevents that.
  useEffect(() => {
    // During hover, inspect item metadata only; actual files and MIME/size
    // validation wait until drop.
    const containsFile = (event: DragEvent): boolean => {
      const items = event.dataTransfer ? event.dataTransfer.items : null;
      if (!items) {
        return false;
      }
      return Array.from(items).some((item) => item.kind === 'file');
    };

    // The default is cancelled only for payloads that actually advertise a file,
    // so a dragged link or a dragged text selection keeps its normal browser
    // behaviour.
    const handleWindowDragOver = (event: DragEvent): void => {
      if (containsFile(event)) {
        event.preventDefault();
      }
    };

    const handleWindowDrop = (event: DragEvent): void => {
      if (containsFile(event)) {
        event.preventDefault();
      }
    };

    // These native DOM DragEvents stay in bubble phase and never stop
    // propagation, so cell handlers still run; observing only dragover/drop
    // leaves Grid's key listener untouched.
    window.addEventListener('dragover', handleWindowDragOver);
    window.addEventListener('drop', handleWindowDrop);
    return () => {
      window.removeEventListener('dragover', handleWindowDragOver);
      window.removeEventListener('drop', handleWindowDrop);
    };
  }, []);

  // Retires the notice after its token-defined lifetime. Re-running on each new
  // rejection clears the previous timer, so a second rejection gets a full
  // lifetime of its own rather than inheriting the remainder of the first.
  useEffect(() => {
    if (!state.rejection) {
      return undefined;
    }
    const timer = window.setTimeout(() => {
      dispatch({ type: 'dismiss-rejection' });
    }, CELL_IMAGE_TOKENS.rejectionNoticeMs);
    return () => window.clearTimeout(timer);
  }, [state.rejection]);

  // Memoized so the value's identity changes only when the map, the pending
  // rejection, or a callback does. That keeps a cell's idle render path free of
  // context churn while it is being typed into.
  const contextValue = useMemo<CellImageContextValue>(
    () => ({
      images: state.images,
      rejection: state.rejection,
      getCellImage,
      setCellImage,
      rejectCellImage,
      clearCellImage,
      clearAllCellImages,
      dismissRejection,
    }),
    [
      state.images,
      state.rejection,
      getCellImage,
      setCellImage,
      rejectCellImage,
      clearCellImage,
      clearAllCellImages,
      dismissRejection,
    ],
  );

  // Render no wrapper box; the conditional polite status region announces
  // rejection without taking focus.
  return (
    <CellImageContext.Provider value={contextValue}>
      {children}
      {state.rejection ? (
        <div role="status" aria-live="polite" style={statusStripStyle}>
          {state.rejection.message}
        </div>
      ) : null}
    </CellImageContext.Provider>
  );
}

// Never throws on a missing provider: the inert default is what lets a cell be
// rendered in isolation with no change in behaviour.
export function useCellImages(): CellImageContextValue {
  return useContext(CellImageContext);
}
