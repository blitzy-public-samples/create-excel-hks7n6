// Cancel dragover so drop can fire, and cancel drop to prevent browser
// navigation.
// Hover can inspect item kind metadata only; MIME and size validation wait until
// drop.
// Only file-system File drags are supported; URL/HTML cross-page drags are
// rejected without network access.
// This hook SELECTS a candidate from the dropped files; the store re-applies the
// same admission rules and owns the object URL, so acceptance is decided in exactly
// one place. Every handler below is synchronous from end to end: a drop is fully
// resolved inside the event that delivered it.

import { useCallback, useMemo, useRef, useState } from 'react';
// React's synthetic DragEvent, which is what a handler attached in JSX receives.
// This type-only import deliberately shadows the same-named DOM global inside
// this module; the store uses that global instead, because its stray-drop guard
// is a native listener rather than a JSX handler.
import type { DragEvent } from 'react';
import { useCellImages } from './cellImageStore';
import { ACCEPTED_IMAGE_MIME_TYPES, MAX_IMAGE_BYTES } from './cellImageTokens';

interface CellImageDragHandlers {
  onDragEnter: (event: DragEvent<HTMLDivElement>) => void;
  onDragOver: (event: DragEvent<HTMLDivElement>) => void;
  onDragLeave: (event: DragEvent<HTMLDivElement>) => void;
  onDrop: (event: DragEvent<HTMLDivElement>) => void;
}

interface UseCellImageDropResult {
  dragHandlers: CellImageDragHandlers;
  isDragActive: boolean;
  isRejecting: boolean;
}

// Guard synthetic/test events without a data store; Array.from supports both
// DataTransferItemList and the test harness's array.
const dragPayloadHasFile = (event: DragEvent<HTMLDivElement>): boolean => {
  const items = event.dataTransfer ? event.dataTransfer.items : null;
  if (!items) {
    return false;
  }
  return Array.from(items).some((item) => item.kind === 'file');
};

// Whether a file even claims to be one of the five raster types this feature takes.
// some(), not includes(): ACCEPTED_IMAGE_MIME_TYPES is a readonly tuple of literal
// types, so the array's own membership test would accept only those five literals
// while a file's type is a plain string.
const hasAcceptedType = (file: File): boolean =>
  ACCEPTED_IMAGE_MIME_TYPES.some((mimeType) => mimeType === file.type);

// Every admission rule the store will apply, mirrored here so that CHOOSING a
// candidate and ACCEPTING it cannot disagree. Both read the same two exported
// constants, and the store remains the sole authority on the decision — this
// predicate only decides which of several files is worth offering it. Judging a
// candidate on the declared type alone is what let an oversized first picture shadow
// a perfectly good one behind it.
const isAcceptableImage = (file: File): boolean =>
  hasAcceptedType(file) && file.size > 0 && file.size <= MAX_IMAGE_BYTES;

// Makes the cursor tell the truth: a copy affordance when this cell will take
// the payload, and none when it will not. Assigning dropEffect is the only
// channel a page has for that, and it has to be reassigned on every hover event
// because the drag operation is renegotiated each time one fires.
const signalDropEffect = (event: DragEvent<HTMLDivElement>, canAccept: boolean): void => {
  if (event.dataTransfer) {
    event.dataTransfer.dropEffect = canAccept ? 'copy' : 'none';
  }
};

// Undefined keys keep the handlers inert so existing Cell consumers need no
// caller-side guard.
export function useCellImageDrop(key: string | undefined): UseCellImageDropResult {
  // Subscribed to this key alone, so a refusal reported for another cell cannot
  // re-render this one.
  const { setCellImage, rejectCellImage, rejection } = useCellImages(key);

  // Depth counter. Moving the pointer from a cell into one of that cell's own
  // child nodes fires an enter for the node being entered and a leave for the
  // node being left, and both bubble to this same handler pair, so a plain
  // boolean would switch the outline off and straight back on mid-gesture.
  // Counting enters against leaves holds the affordance steady for as long as
  // the pointer is anywhere inside the cell.
  const dragDepthRef = useRef<number>(0);

  // Mutating a ref does not re-render, so the derived flag is mirrored into
  // state while the ref remains the source of truth for the arithmetic.
  const [isDragActive, setIsDragActive] = useState<boolean>(false);

  // Derived from the provider's pending rejection rather than held here, so the
  // feature keeps exactly one dismissal timer. The cell's outline and the
  // application-level notice therefore appear and disappear together, and this
  // hook leaves behind no timer that could fire after the cell has unmounted.
  // The subscription is already scoped to this key, so a non-null value here can
  // only ever be this cell's own refusal.
  const isRejecting = rejection !== null;

  const onDragEnter = useCallback(
    (event: DragEvent<HTMLDivElement>): void => {
      event.preventDefault();

      const canAccept = key !== undefined && dragPayloadHasFile(event);
      signalDropEffect(event, canAccept);
      if (!canAccept) {
        return;
      }

      dragDepthRef.current += 1;
      setIsDragActive(true);
    },
    [key],
  );

  const onDragOver = useCallback(
    (event: DragEvent<HTMLDivElement>): void => {
      // The single most consequential line in this file: without it the browser
      // never delivers a drop to this element.
      event.preventDefault();
      signalDropEffect(event, key !== undefined && dragPayloadHasFile(event));
    },
    [key],
  );

  // Do not re-inspect the payload on leave: a user agent may withhold the item
  // list on the way out, and reading that as "no file" would flicker the
  // affordance off while the pointer is still inside the cell.
  const onDragLeave = useCallback((_event: DragEvent<HTMLDivElement>): void => {
    // Clamped at zero so a leave that no enter ever matched — a non-file drag
    // passing through, for instance — cannot drive the count negative and leave
    // the affordance stuck on for the rest of the session.
    const nextDepth = Math.max(0, dragDepthRef.current - 1);
    dragDepthRef.current = nextDepth;
    setIsDragActive(nextDepth > 0);
  }, []);

  const onDrop = useCallback(
    (event: DragEvent<HTMLDivElement>): void => {
      // Cancelled first, before anything can fail: without this the browser
      // leaves the application to display the dropped image and none of the
      // work below is ever observed.
      event.preventDefault();

      // The gesture is over however it ended, so the count is reset outright
      // rather than decremented — a drop consumes every outstanding enter.
      dragDepthRef.current = 0;
      setIsDragActive(false);

      if (key === undefined) {
        return;
      }

      const files = event.dataTransfer ? Array.from(event.dataTransfer.files) : [];
      if (files.length === 0) {
        // Silent no-op, and the path a cross-page drag takes: it carries URL or
        // markup strings rather than a File. There is nothing to announce
        // because the user did nothing wrong, and no cell is touched.
        return;
      }

      // The first ACCEPTABLE image file wins and the rest are ignored: fanning
      // out across neighbouring cells would need grid geometry and would change
      // selection semantics. Acceptable means every admission rule, not merely the
      // allow-list — a selection led by an oversized or empty picture must not
      // shadow a smaller, perfectly good one further down it.
      const acceptable = files.find(isAcceptableImage);

      // Nothing in the selection can be accepted, so fall back to the first file
      // that at least claims a raster type this feature takes, and let the store
      // say precisely what is wrong with it. Handing it over rather than composing
      // the refusal here is what keeps acceptance decided in exactly one place: the
      // store re-checks everything before it allocates, so a file that fails comes
      // back as a rejection carrying its own reason instead of an image.
      const candidate = acceptable ?? files.find(hasAcceptedType);

      if (candidate) {
        // The call returns nothing to wait for: the picture — or the refusal — is
        // committed in this same event.
        setCellImage(key, candidate);
        return;
      }

      // Files were dropped, but not one of them claims a type this feature accepts.
      // The notice names the first, which is the only file the user can be assumed
      // to have meant.
      rejectCellImage(key, 'unsupported-type', files[0].name);
    },
    [key, rejectCellImage, setCellImage],
  );

  const dragHandlers = useMemo<CellImageDragHandlers>(
    () => ({ onDragEnter, onDragOver, onDragLeave, onDrop }),
    [onDragEnter, onDragOver, onDragLeave, onDrop],
  );

  return useMemo<UseCellImageDropResult>(
    () => ({ dragHandlers, isDragActive, isRejecting }),
    [dragHandlers, isDragActive, isRejecting],
  );
}
