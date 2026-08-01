// Cancel dragover so drop can fire and cancel drop to prevent file navigation. Hover inspects item
// kinds only; File/MIME/size handling waits until drop. Cross-page URL/HTML drags are unsupported.

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

// Compare File.type with .some because the allow-list is a readonly tuple while File.type is a
// general string.
const hasAcceptedType = (file: File): boolean =>
  ACCEPTED_IMAGE_MIME_TYPES.some((mimeType) => mimeType === file.type);

// Mirror the store's metadata gates when choosing among several files so an invalid leading raster
// cannot hide a later acceptable one; the store still makes the final admission decision.
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

  // Count bubbling enter/leave pairs so crossing child nodes cannot flicker the cell-level affordance.
  const dragDepthRef = useRef<number>(0);

  // Mutating a ref does not re-render, so the derived flag is mirrored into
  // state while the ref remains the source of truth for the arithmetic.
  const [isDragActive, setIsDragActive] = useState<boolean>(false);

  // Reuse the provider's key-scoped rejection and timer so the cell outline and page-level notice
  // retire together.
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
      // A drop target must cancel dragover or the browser will not dispatch drop.
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
      // Cancel before validation so a rejected or failed drop cannot navigate the browser away.
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
        // URL/HTML cross-page drags carry no File, so they are unsupported silent no-ops.
        return;
      }

      // Choose the first file satisfying every metadata gate; do not fan out across neighbouring cells.
      const acceptable = files.find(isAcceptableImage);

      // If none is acceptable, pass the first allow-listed raster to the store for a precise
      // size/empty-file rejection; the store revalidates before minting.
      const candidate = acceptable ?? files.find(hasAcceptedType);

      if (candidate) {
        setCellImage(key, candidate);
        return;
      }

      // No file claims an accepted raster type; report the first dropped file.
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
