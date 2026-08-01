// Keeps blob URLs outside Redux/persisted workbook state. URL ownership is updated synchronously and
// released centrally; key-scoped useSyncExternalStore snapshots prevent unrelated cells from
// re-rendering.
import {
  createContext,
  useCallback,
  useContext,
  useLayoutEffect,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useSyncExternalStore,
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

// The figure the notice quotes is derived from the token that enforces it, so a
// limit and its explanation cannot disagree.
const BYTES_PER_MEBIBYTE = 1024 * 1024;
const MAX_IMAGE_MEBIBYTES = MAX_IMAGE_BYTES / BYTES_PER_MEBIBYTE;

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

// Context carries stable operations/readers rather than observable state, so provider commits do not
// broadcast React context updates. Undefined keys remain inert.
interface CellImageStoreApi {
  subscribe: (onStoreChange: () => void) => () => void;
  getCellImage: (key: string | undefined) => CellImageEntry | undefined;
  getCellRejection: (key: string | undefined) => CellImageRejection | null;
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

interface CellImageAccess {
  image: CellImageEntry | undefined;
  rejection: CellImageRejection | null;
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

// Couples a machine-readable reason with a specific notice detail; zero-byte allow-listed files reuse
// 'unsupported-type' without telling the user their declared format is unsupported.
interface CellImageRefusal {
  reason: CellImageRejectionReason;
  detail: string;
}

// A zero-byte file contains no image data, so reject it before creating an object URL; no
// container-content claim is made for non-empty files.
const EMPTY_FILE_DETAIL = 'it is empty, so it carries no image data';

// Validate declared MIME type and File.size before creating an object URL; return the refusal or null.
function refusalFor(file: File): CellImageRefusal | null {
  // some(), not includes(): ACCEPTED_IMAGE_MIME_TYPES is a readonly tuple of
  // literal types, so its own membership test would accept only those five
  // literals while File.type is a plain string.
  const isAcceptedType = ACCEPTED_IMAGE_MIME_TYPES.some((mimeType) => mimeType === file.type);
  if (!isAcceptedType) {
    return { reason: 'unsupported-type', detail: rejectionDetail('unsupported-type') };
  }
  // Refused with the type reason rather than the size one: an empty file is not too
  // large, it is not an image of the type it claims to be.
  if (file.size === 0) {
    return { reason: 'unsupported-type', detail: EMPTY_FILE_DETAIL };
  }
  if (file.size > MAX_IMAGE_BYTES) {
    return { reason: 'too-large', detail: rejectionDetail('too-large') };
  }
  return null;
}

// Keep URL, timestamp, timer, and event side effects outside this reducer. A branch
// that changes nothing returns the received state untouched, identity included, so a
// transition with no effect costs no render anywhere in the grid.
function cellImageReducer(state: CellImageState, action: CellImageAction): CellImageState {
  switch (action.type) {
    case 'set':
      return {
        images: { ...state.images, [action.key]: action.entry },
        // A successful drop clears only a rejection for the same key; another cell's notice remains.
        rejection:
          state.rejection !== null && state.rejection.key === action.key ? null : state.rejection,
      };
    case 'clear': {
      // Preserve state identity so a no-op clear does not notify every key-scoped subscriber.
      if (!(action.key in state.images)) {
        return state;
      }
      const next: CellImageMap = { ...state.images };
      delete next[action.key];
      return { images: next, rejection: state.rejection };
    }
    case 'clearAll':
      if (Object.keys(state.images).length === 0) {
        return state;
      }
      return { images: {}, rejection: state.rejection };
    case 'reject':
      return { images: state.images, rejection: action.rejection };
    case 'dismiss-rejection':
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
    default:
      return 'it could not be accepted';
  }
}

// Callers may use the reason's default detail; the admission gate can supply a more specific message
// for zero-byte files.
function buildRejection(
  key: CellImageKey,
  reason: CellImageRejectionReason,
  fileName: string,
  detail: string = rejectionDetail(reason),
): CellImageRejection {
  // A dragged payload can legitimately carry an empty name, so fall back to a
  // neutral label rather than announcing a blank.
  const label = fileName.length > 0 ? fileName : 'the dropped file';
  return { key, reason, fileName, message: `Skipped ${label}: ${detail}.` };
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

// Nothing ever changes without a provider, so the inert store hands back a
// subscription that never fires. Returning a shared no-op unsubscribe keeps its
// identity stable too, which is what useSyncExternalStore needs to avoid
// resubscribing on every render.
const NEVER_NOTIFIES = (): void => undefined;

// The default is a working inert implementation rather than undefined, so a cell
// renders identically whether or not a provider is mounted above it and mounting
// the provider never becomes a prerequisite for an existing page or test.
const NO_OP_CELL_IMAGE_STORE: CellImageStoreApi = Object.freeze({
  subscribe: () => NEVER_NOTIFIES,
  getCellImage: () => undefined,
  getCellRejection: () => null,
  setCellImage: () => undefined,
  rejectCellImage: () => undefined,
  clearCellImage: () => undefined,
  clearAllCellImages: () => undefined,
  dismissRejection: () => undefined,
});

const CellImageContext = createContext<CellImageStoreApi>(NO_OP_CELL_IMAGE_STORE);

export function CellImageProvider({ children }: CellImageProviderProps): JSX.Element {
  const [state, dispatch] = useReducer(cellImageReducer, INITIAL_CELL_IMAGE_STATE);

  // The canonical ownership registry, mapping a cell key to the object URL held
  // for it: written synchronously wherever a URL is minted or released, so every
  // release path sees what is owned at the instant it runs rather than at the
  // last commit.
  const ownedRef = useRef<Map<CellImageKey, string>>(new Map());

  // Every object URL minted and not yet released. Membership is what makes release
  // idempotent and what tells the unmount sweep which URLs are still outstanding.
  const liveUrlsRef = useRef<Set<string>>(new Set());

  // The published snapshot: the reducer state as of the last COMMIT. Subscribers
  // read this rather than the rendered closure, which is what lets a cell read its
  // own slice without this provider having to hand the whole state down.
  const publishedRef = useRef<CellImageState>(state);

  // Subscribers, keyed by nothing: every listener is notified and each one decides
  // for itself whether its own slice moved. That decision is a single Object.is
  // comparison inside React, so an unaffected cell costs a comparison rather than
  // a render.
  const listenersRef = useRef<Set<() => void>>(new Set());

  // Publish only committed state in a layout effect so subscribers see the new snapshot before paint;
  // identity-preserving no-op transitions skip notification.
  useLayoutEffect(() => {
    publishedRef.current = state;
    listenersRef.current.forEach((listener) => {
      listener();
    });
  }, [state]);

  // Stable for the provider's lifetime: useSyncExternalStore resubscribes whenever
  // this identity changes, and a subscription that churned every commit would
  // reintroduce exactly the per-cell work this design removes.
  const subscribe = useCallback((onStoreChange: () => void): (() => void) => {
    const listeners = listenersRef.current;
    listeners.add(onStoreChange);
    return () => {
      listeners.delete(onStoreChange);
    };
  }, []);

  // Delete from the live set before revoking so repeated release attempts are harmless.
  const releaseObjectUrl = useCallback((objectUrl: string): void => {
    if (!liveUrlsRef.current.delete(objectUrl)) {
      return;
    }
    URL.revokeObjectURL(objectUrl);
  }, []);

  // Both readers are snapshot reads of the published state, which is what makes
  // them safe as a useSyncExternalStore getSnapshot: the value returned for an
  // unchanged key is the identical object every time, so React sees no change.
  const getCellImage = useCallback((key: string | undefined): CellImageEntry | undefined => {
    if (key === undefined) {
      return undefined;
    }
    const images = publishedRef.current.images;
    // Indexed access is not checked by this project's compiler settings, so
    // presence is tested explicitly instead of trusting the read.
    return key in images ? images[key] : undefined;
  }, []);

  const getCellRejection = useCallback((key: string | undefined): CellImageRejection | null => {
    if (key === undefined) {
      return null;
    }
    const rejection = publishedRef.current.rejection;
    return rejection !== null && rejection.key === key ? rejection : null;
  }, []);

  // Validate declared MIME type and File.size before minting an object URL. Rejections may retain a
  // small notice record, but they add no blob URL to the image map.
  const setCellImage = useCallback(
    (key: string | undefined, file: File): void => {
      if (key === undefined) {
        return;
      }

      const refusal = refusalFor(file);
      if (refusal !== null) {
        dispatch({
          type: 'reject',
          rejection: buildRejection(key, refusal.reason, file.name, refusal.detail),
        });
        return;
      }

      const supersededUrl = ownedRef.current.get(key);
      const objectUrl = URL.createObjectURL(file);
      liveUrlsRef.current.add(objectUrl);
      // Ownership is recorded BEFORE the dispatch that renders it, and this same
      // assignment removes the superseded URL from the registry, so the URL
      // about to be revoked is already unreachable.
      ownedRef.current.set(key, objectUrl);

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

      // After ownership switches to the replacement, release the superseded URL; the live-set guard
      // prevents duplicate revocation.
      if (supersededUrl !== undefined) {
        releaseObjectUrl(supersededUrl);
      }
    },
    [releaseObjectUrl],
  );

  // Records a hook-originated rejection without minting or releasing an object URL; the image map is
  // unchanged.
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
      const ownedUrl = ownedRef.current.get(key);
      // Ownership is dropped before the URL is released, so a released URL is never
      // reachable from the registry and a repeated clear cannot revoke it twice.
      ownedRef.current.delete(key);
      dispatch({ type: 'clear', key });
      // Release path (b): the explicit dismiss. The cell's own value and formula
      // are untouched, so removing the image simply reveals them again.
      if (ownedUrl !== undefined) {
        releaseObjectUrl(ownedUrl);
      }
    },
    [releaseObjectUrl],
  );

  const clearAllCellImages = useCallback((): void => {
    // Snapshot, then empty the registry, then revoke: after the clear no
    // released URL is reachable.
    const releasedUrls = Array.from(ownedRef.current.values());
    ownedRef.current.clear();
    dispatch({ type: 'clearAll' });
    // Release path (c): every URL the registry held, each released exactly once.
    releasedUrls.forEach((objectUrl) => {
      releaseObjectUrl(objectUrl);
    });
  }, [releaseObjectUrl]);

  const dismissRejection = useCallback((): void => {
    dispatch({ type: 'dismiss-rejection' });
  }, []);

  // Release path (d): sweep outstanding URLs when the provider unmounts. releaseObjectUrl is stable,
  // so this effect installs once.
  useEffect(() => {
    // The registries are created once and mutated in place, so captured identities still expose all
    // URLs held when cleanup executes.
    const owned = ownedRef.current;
    const live = liveUrlsRef.current;
    return () => {
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

  // Every dependency below is stable for the provider's lifetime, so this value is
  // built once and never changes identity. That is the whole point: a context whose
  // value never changes cannot wake a consumer, so the only thing that can re-render
  // a cell is that cell's own subscription.
  const storeApi = useMemo<CellImageStoreApi>(
    () => ({
      subscribe,
      getCellImage,
      getCellRejection,
      setCellImage,
      rejectCellImage,
      clearCellImage,
      clearAllCellImages,
      dismissRejection,
    }),
    [
      subscribe,
      getCellImage,
      getCellRejection,
      setCellImage,
      rejectCellImage,
      clearCellImage,
      clearAllCellImages,
      dismissRejection,
    ],
  );

  // Render no wrapper box; the conditional polite status region announces
  // rejection without taking focus. The notice is read from this provider's own
  // state, which is what keeps it an application-level concern instead of
  // something every cell has to subscribe to.
  return (
    <CellImageContext.Provider value={storeApi}>
      {children}
      {state.rejection ? (
        <div role="status" aria-live="polite" style={statusStripStyle}>
          {state.rejection.message}
        </div>
      ) : null}
    </CellImageContext.Provider>
  );
}

// Returns key-scoped image/rejection snapshots plus stable operations. With no provider or no key, the
// inert defaults remain constant and do not trigger renders.
export function useCellImages(key?: string): CellImageAccess {
  const {
    subscribe,
    getCellImage,
    getCellRejection,
    setCellImage,
    rejectCellImage,
    clearCellImage,
    clearAllCellImages,
    dismissRejection,
  } = useContext(CellImageContext);

  // Bound to this key so the snapshot React caches is this cell's own slice. Both
  // readers return the stored object itself, so an unrelated change reads back the
  // identical reference and React skips the render entirely.
  const readImage = useCallback((): CellImageEntry | undefined => getCellImage(key), [
    getCellImage,
    key,
  ]);
  const readRejection = useCallback((): CellImageRejection | null => getCellRejection(key), [
    getCellRejection,
    key,
  ]);

  const image = useSyncExternalStore(subscribe, readImage);
  const rejection = useSyncExternalStore(subscribe, readRejection);

  return useMemo<CellImageAccess>(
    () => ({
      image,
      rejection,
      setCellImage,
      rejectCellImage,
      clearCellImage,
      clearAllCellImages,
      dismissRejection,
    }),
    [
      image,
      rejection,
      setCellImage,
      rejectCellImage,
      clearCellImage,
      clearAllCellImages,
      dismissRejection,
    ],
  );
}
