// Ephemeral in-memory store for images dropped onto spreadsheet cells.
//
// WHY THIS IS NOT A REDUX SLICE. Cell, Worksheet and Workbook in
// src/schema/workbookTypes.ts are the shapes that cross the REST boundary and
// feed the Firestore sync path, so an image field on any of them would silently
// change a persisted contract. Holding the map in a parallel, non-persisted
// React context is what makes "the images do not need to be saved anywhere"
// literally true: nothing here reaches the Redux store, browser storage, a
// request body, or a remote document. Practically, src/store/index.ts also
// exports no typed hook bindings for reading or writing store state, so a
// store-based design would have had to import symbols that do not exist.
//
// LIFETIME. The provider mounts above the router, so the map survives component
// remounts and client-side route changes; a hard refresh or a tab close destroys
// it, which the experiment's brief accepts explicitly.
//
// OBJECT-URL INVARIANT. Every object URL this store mints is released exactly
// once, across four release paths: (a) a replacement image on the same key,
// (b) an explicit clear, (c) clearAllCellImages, and (d) provider unmount. The
// store mints at one call site and releases at four, one per path, so the
// pairing stays auditable by inspection rather than by reasoning. An
// unpaired object URL pins its blob for the document's lifetime, which would
// corrupt the very memory behaviour this experiment exists to observe. Note the
// deliberate departure from the common preview idiom that revokes inside the
// image's load handler: the same URL has to stay valid across every re-render,
// so release is driven by ownership changes instead.
//
// PURITY BOUNDARY. The reducer is pure. Object URLs, timestamps, timers and
// event listeners live exclusively in the callbacks and effects outside it.

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
import { ACCEPTED_IMAGE_MIME_TYPES, CELL_IMAGE_TOKENS, MAX_IMAGE_BYTES } from './cellImageTokens';

// Human-readable format list for the rejection notice, derived from the
// allow-list so the wording can never drift from what validation accepts.
const ACCEPTED_IMAGE_LABELS = ACCEPTED_IMAGE_MIME_TYPES.map((mimeType) =>
  mimeType.slice(mimeType.indexOf('/') + 1).toUpperCase(),
).join(', ');

// Mebibyte ceiling for the same notice, derived from the byte ceiling so the two
// cannot disagree either.
const MAX_IMAGE_MEBIBYTES = MAX_IMAGE_BYTES / 1024 / 1024;

// Presentation for the single rejection notice. The client ships no stylesheet,
// so the notice carries its own styling; every colour and layer value resolves
// through CELL_IMAGE_TOKENS and the remaining entries are structural keywords
// that carry no design decision. Viewport-fixed placement is used because no
// element in the application shell establishes a containing block. Logical inset
// properties keep the placement correct under a right-to-left writing mode.
// Disabling pointer events guarantees the notice never intercepts a click aimed
// at the grid beneath it, so an in-flight experiment is not interrupted while
// the notice is on screen, and text wrapping keeps a long file name inside the
// strip instead of overflowing it.
// BLITZY [DESIGN_SYSTEM_GAP]: the feature's token set defines a surface colour
// and a text colour for this strip but no spacing token, and the repository has
// no spacing scale to inherit. Padding is therefore omitted rather than invented
// as a literal; a designer-supplied spacing token would slot straight in here.
// The explicit annotation is required: without it these keyword strings widen to
// string and fail against the closed unions in React's style typings.
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

// Reducer state. The pending rejection travels alongside the map because
// recording and dismissing a rejection are transitions of this same reducer.
interface CellImageState {
  images: CellImageMap;
  rejection: CellImageRejection | null;
}

// Every transition the store supports. Object URLs and timestamps are computed
// by the callbacks and arrive here as plain data, which is what keeps the
// reducer pure and trivially testable.
type CellImageAction =
  | { type: 'set'; key: CellImageKey; entry: CellImageEntry }
  | { type: 'clear'; key: CellImageKey }
  | { type: 'clearAll' }
  | { type: 'reject'; rejection: CellImageRejection }
  | { type: 'dismiss-rejection' };

// The context surface consumed by the drop hook, the overlay and the cell. Every
// key-taking member tolerates undefined and no-ops on it, so no consumer is ever
// forced to narrow a possibly-absent cell key before calling in.
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

// Pure: no object URL, no timestamp, no logging, no timer, no I/O of any kind.
// Every branch returns a freshly built state object, so no caller can observe a
// mutation of the previous one.
function cellImageReducer(state: CellImageState, action: CellImageAction): CellImageState {
  switch (action.type) {
    case 'set':
      // A successful drop also clears any notice still on screen: the complaint
      // the user just acted on should not outlive the correction.
      return { images: { ...state.images, [action.key]: action.entry }, rejection: null };
    case 'clear': {
      const next: CellImageMap = { ...state.images };
      delete next[action.key];
      return { images: next, rejection: state.rejection };
    }
    case 'clearAll':
      return { images: {}, rejection: state.rejection };
    case 'reject':
      return { images: state.images, rejection: action.rejection };
    case 'dismiss-rejection':
      return { images: state.images, rejection: null };
    default:
      return state;
  }
}

// Builds a rejection record, deriving the message from the reason so the two can
// never disagree. Pure: it mints no object URL and touches no cell data.
function buildRejection(
  key: CellImageKey,
  reason: CellImageRejectionReason,
  fileName: string,
): CellImageRejection {
  // A dragged payload can legitimately carry an empty name, so fall back to a
  // neutral label rather than announcing a blank.
  const label = fileName.length > 0 ? fileName : 'the dropped file';
  const message =
    reason === 'too-large'
      ? `Skipped ${label}: an image dropped into a cell must be under ${MAX_IMAGE_MEBIBYTES} MiB.`
      : `Skipped ${label}: only ${ACCEPTED_IMAGE_LABELS} images can be dropped into a cell.`;
  return { key, reason, fileName, message };
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

// The default is a working inert implementation rather than undefined, and that
// is load-bearing: a cell renders identically whether or not a provider is
// mounted above it, so mounting the provider never becomes a hard prerequisite
// for an existing page or test. Parameters are omitted rather than declared and
// ignored, which documents the inertness and satisfies the unused-parameter
// check at the same time.
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

// Owns the ephemeral map, the whole object-URL lifecycle, the window-level guard
// against a stray file drop, and the single accessible notice region. Takes
// children only, so it can be mounted at application level with no wiring.
export function CellImageProvider({ children }: CellImageProviderProps): JSX.Element {
  const [state, dispatch] = useReducer(cellImageReducer, INITIAL_CELL_IMAGE_STATE);

  // Mirror of the live map, resynchronized on every render. Release paths read
  // through it so they observe the current map rather than the one captured when
  // the callback or the unmount cleanup was created — without this, the unmount
  // sweep would revoke the map as it looked at mount time and leak everything
  // dropped afterwards.
  const mapRef = useRef<CellImageMap>(state.images);
  mapRef.current = state.images;

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

  // Validates first and only then mints an object URL, so a rejected payload
  // never allocates one. The allow-list is matched with some() rather than
  // includes(): the list is a readonly tuple of literal types, and includes()
  // would only accept those five literals while File.type is a plain string.
  const setCellImage = useCallback((key: string | undefined, file: File): void => {
    if (key === undefined) {
      return;
    }

    const isAcceptedType = ACCEPTED_IMAGE_MIME_TYPES.some((accepted) => accepted === file.type);
    if (!isAcceptedType) {
      // Raster-only by design. Scriptable image formats are refused outright
      // rather than sanitized, and the cell is left exactly as it was.
      dispatch({ type: 'reject', rejection: buildRejection(key, 'unsupported-type', file.name) });
      return;
    }

    if (file.size > MAX_IMAGE_BYTES) {
      // An unbounded retained blob is a memory-exhaustion vector, so the ceiling
      // is enforced before anything is allocated.
      dispatch({ type: 'reject', rejection: buildRejection(key, 'too-large', file.name) });
      return;
    }

    // Captured before the dispatch, because the mirror is resynchronized on the
    // render that the dispatch triggers.
    const previous = key in mapRef.current ? mapRef.current[key] : undefined;
    const objectUrl = URL.createObjectURL(file);

    dispatch({
      type: 'set',
      key,
      entry: {
        objectUrl,
        fileName: file.name,
        mimeType: file.type,
        sizeBytes: file.size,
        droppedAt: Date.now(),
      },
    });

    // Release path (a): the superseded URL is revoked exactly once, after the
    // replacement is in flight, so no render ever points at a revoked blob.
    if (previous) {
      URL.revokeObjectURL(previous.objectUrl);
    }
  }, []);

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

  const clearCellImage = useCallback((key: string | undefined): void => {
    if (key === undefined) {
      return;
    }
    const existing = key in mapRef.current ? mapRef.current[key] : undefined;
    dispatch({ type: 'clear', key });
    // Release path (b): the explicit dismiss. The cell's own value and formula
    // are untouched, so removing the image simply reveals them again.
    if (existing) {
      URL.revokeObjectURL(existing.objectUrl);
    }
  }, []);

  const clearAllCellImages = useCallback((): void => {
    // Snapshot before the dispatch: after it, the mirror no longer holds them.
    const released = Object.values(mapRef.current);
    dispatch({ type: 'clearAll' });
    // Release path (c): every URL the map held, each revoked exactly once.
    released.forEach((entry) => {
      URL.revokeObjectURL(entry.objectUrl);
    });
  }, []);

  const dismissRejection = useCallback((): void => {
    dispatch({ type: 'dismiss-rejection' });
  }, []);

  // Release path (d): the unmount sweep. The empty dependency list is required so
  // the cleanup runs only when the provider goes away, and the mirror is what
  // makes it see everything still held at that moment.
  useEffect(
    () => () => {
      Object.values(mapRef.current).forEach((entry) => {
        URL.revokeObjectURL(entry.objectUrl);
      });
    },
    [],
  );

  // Guard against a stray file drop. A browser handles a dropped file by default
  // — opening or downloading it — even when the drop lands outside a registered
  // target, so a drop that misses a cell by a few pixels would navigate away from
  // the application and end the experiment mid-assessment. Cancelling the default
  // at window level prevents that.
  useEffect(() => {
    // Acceptance during the hover phase has to be decided from the item kinds:
    // the drag data store is readable only while a drag starts or drops.
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

    // Bubble phase, and propagation is never halted, so the cell-level handlers
    // still receive every event they need. Only these two drag events are
    // observed, so this guard cannot interfere with the grid's existing window
    // keyboard listener or with its arrow-key navigation.
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

  // The provider element itself renders no box, and the notice is present only
  // while a rejection is, so an idle application shell is laid out exactly as it
  // is without this feature. The notice is the one polite live region the feature
  // owns: it announces the reason without interrupting, and it never takes focus
  // away from the grid.
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

// Reads the store. Deliberately never throws on a missing provider: the inert
// default makes the provider optional, which is what lets a cell be rendered in
// isolation — in a test or on a page that has not been wrapped — with no change
// in behaviour.
export function useCellImages(): CellImageContextValue {
  return useContext(CellImageContext);
}

