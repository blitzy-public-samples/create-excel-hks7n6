// Keep image blobs outside the workbook/Redux model so they remain page-scoped
// and never enter persisted payloads.
// This provider owns each object URL until replacement, explicit clear,
// clear-all, or unmount; URLs stay valid across React renders, so they are not
// revoked on image load.
// Ingestion is SYNCHRONOUS and zero-copy: a dropped file is checked against the
// raster allow-list and against the encoded length it reports — which must be
// non-zero and within the per-file ceiling — and the very next statement
// mints its object URL. Nothing is read, parsed, or decoded first, because the
// experiment being run is a visual assessment of dropping pictures into cells,
// and main-thread work between the drop and the paint would measure this store
// instead of the spreadsheet. It also means there is no in-flight state at all:
// every drop is decided, committed, and rendered within the event that caused it,
// so a later drop can never be overtaken by an earlier one.
// Ownership lives in a ref that is written synchronously, never derived from
// rendered state: React commits state asynchronously, so a record read from a
// render would be stale for the same-task sequences that orphan a URL or revoke
// one twice.
// RENDER SCOPING. A spreadsheet renders a cell per visible position, so anything
// that broadcasts to every cell costs O(cells) per drop. Nothing observable is
// therefore carried in the context value: it holds operations only, all of which
// keep one identity for the provider's whole lifetime, so a context consumer can
// never be woken by a state change. Cells read their OWN picture and their OWN
// refusal through useSyncExternalStore, whose snapshot for a key is the very entry
// object stored under it. A change to another key leaves that snapshot reference
// untouched, so React finds nothing new and re-renders nothing. The reducer
// remains the single authority for what is held; the subscription layer only
// publishes what it has already committed.
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

// What the context carries: operations and the two key-scoped readers the
// subscription hook needs, and deliberately NOT the map or the pending rejection.
// Every member keeps one identity for the provider's lifetime, which is what makes
// this context structurally incapable of broadcasting a state change.
// Key-based operations accept undefined so cells without an imageKey remain
// inert without caller-side guards.
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

// What a consumer of useCellImages receives: the picture held for the key it asked
// about, the refusal attributed to that same key, and the operations. Both values
// are subscriptions scoped to that one key, so a consumer is woken only by a change
// that concerns it.
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

// A refusal, carrying the reason from the frozen public union together with the
// phrase the notice should use. The phrase travels separately because one reason can
// describe two different payloads — a file whose declared type is not on the
// allow-list, and a file whose declared type IS on it but which carries no bytes at
// all — and the user is owed the distinction even though the machine-readable
// vocabulary stays a two-value contract.
interface CellImageRefusal {
  reason: CellImageRejectionReason;
  detail: string;
}

// A zero-length payload cannot decode into a picture whatever it declares itself to
// be, so it is refused rather than admitted and left to render broken. Length is the
// one thing a metadata-only gate can honestly say about a file's CONTENT: it is
// observable without reading a byte, whereas a container signature is not.
const EMPTY_FILE_DETAIL = 'it is empty, so it carries no image data';

// Everything this feature checks about a dropped file: its declared type against the
// allow-list, then its declared length against zero and against the per-file ceiling.
// Both kinds of value are metadata the platform has already parsed, so validation is
// free and can therefore run before an object URL exists rather than after. Returns
// the refusal, or null when the file is admitted.
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
        // A successful drop retires only the notice that belongs to the SAME
        // cell: the complaint the user just corrected should not outlive the
        // correction, but a refusal reported for another cell is not this
        // drop's to erase.
        rejection:
          state.rejection !== null && state.rejection.key === action.key ? null : state.rejection,
      };
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
    default:
      return 'it could not be accepted';
  }
}

// The detail defaults to the reason's own phrase, so a caller that knows only the
// reason — the drop hook, echoing a payload it refused before this store saw a File —
// needs to supply nothing, while the admission gate can pass the more specific phrase
// it has.
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

  // Publish on commit, never during render, so the snapshot a subscriber reads is
  // always state React has actually committed. A layout effect rather than a
  // passive one, so the notification lands in the same frame as the drop that
  // caused it and no cell ever paints a picture one frame late. The reducer returns
  // its state untouched for a mutation that changes nothing, so this effect does
  // not even run in that case.
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

  // The pending refusal, but only when it belongs to the key being asked about. The
  // notice itself is the provider's own business, rendered from its own state below,
  // so one cell's refusal never reaches another cell at all.
  const getCellRejection = useCallback((key: string | undefined): CellImageRejection | null => {
    if (key === undefined) {
      return null;
    }
    const rejection = publishedRef.current.rejection;
    return rejection !== null && rejection.key === key ? rejection : null;
  }, []);

  // Accepts a file for one cell, or refuses it with a reason. Every check completes
  // before an object URL exists, so a refused payload allocates nothing at all.
  // Synchronous from end to end: the picture is on screen in the same commit as
  // the drop that carried it.
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

      // Release path (a): the superseded URL is released exactly once, after the
      // replacement is in flight, so no render ever points at a revoked blob.
      if (supersededUrl !== undefined) {
        releaseObjectUrl(supersededUrl);
      }
    },
    [releaseObjectUrl],
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

  // Release path (d): the unmount sweep. The empty dependency list is required
  // so the cleanup runs only when the provider goes away.
  useEffect(() => {
    // Both registries are created once and only ever mutated in place, so binding
    // their identities here still observes everything held at the moment the
    // cleanup RUNS, including a URL minted since the last commit.
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

// Reads one cell's ephemeral picture and one cell's refusal, plus the operations.
// Never throws on a missing provider: the inert default is what lets a cell be
// rendered in isolation with no change in behaviour.
// The key is optional because a cell may legitimately have none, and because a
// caller that only needs the operations — a control that clears everything, for
// instance — should not have to invent one. With no key both subscriptions return a
// constant, so such a caller never re-renders at all.
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
