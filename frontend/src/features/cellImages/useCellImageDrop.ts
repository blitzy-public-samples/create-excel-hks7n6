// useCellImageDrop turns any cell box into a drop target for a single image file
// dragged in from the operating system. It returns the four drag handlers that
// Cell.tsx spreads onto its root element, together with the two flags that drive
// the cell's transient affordance: a dashed outline while an acceptable payload
// hovers, and a rejection outline when the last drop on that cell was refused.
//
// WHY THE DEFAULT ACTION IS CANCELLED TWICE, AND WHY IT IS EASY TO DELETE BY
// ACCIDENT. Cancelling the default on dragover is what makes the drop event fire
// at all: leave it out and the browser treats the element as a non-target, so no
// drop is ever delivered and the entire feature silently does nothing.
// Cancelling it again on drop is what stops the browser from leaving the
// application in order to display the dropped image, which would end the
// experiment mid-assessment. Neither omission produces an error message of any
// kind, which is exactly why both are called out here.
//
// WHY HOVER-PHASE ACCEPTANCE READS ONLY THE ITEM KINDS. A drag's data store is
// protected while the drag is merely in progress: it is readable when the drag
// starts and again when it drops, and at no point in between. During dragenter
// and dragover a page may therefore see how many items are coming and what kind
// each one is, but never a file's name, type or size. Acceptance during hover is
// decided from dataTransfer.items[].kind === 'file' for precisely that reason,
// and the allow-list check waits for the drop, which is the first moment
// dataTransfer.files can be read at all.
//
// SCOPE: FILE-SYSTEM DRAGS ONLY. An image dragged out of another page or another
// tab arrives as a text/uri-list or text/html string rather than as a File, and
// honouring it would mean issuing a cross-origin network request for the bytes,
// which this experiment does not do. Such payloads advertise no file, so they
// are told the truth through dropEffect 'none' and their drop is a no-op:
// nothing is requested and no cell is touched.
//
// PURITY BOUNDARY. This hook allocates no blob URL and releases none. It hands
// the selected File to the store and lets the store own that entire lifecycle,
// so an unreleased URL can never originate here. Its only side effects are
// cancelling an event's default action, telling the cursor the truth through
// dropEffect, and moving its own depth counter. It observes no window and no
// document event, so it cannot shadow the grid's existing arrow-key navigation;
// it reaches no Redux store, no browser storage and no remote service; and it
// neither reads nor writes a cell's value or formula, because a dropped image is
// a visual layer and nothing more.

import { useCallback, useMemo, useRef, useState } from 'react';
// React's synthetic DragEvent, which is what a handler attached in JSX receives.
// This type-only import deliberately shadows the same-named DOM global inside
// this module; the store uses that global instead, because its stray-drop guard
// is a native listener rather than a JSX handler.
import type { DragEvent } from 'react';
import { useCellImages } from './cellImageStore';
import { ACCEPTED_IMAGE_MIME_TYPES } from './cellImageTokens';

// Exactly the four keys React's DOM attributes recognise and nothing else, so a
// cell can spread the object straight onto its root element. Declared locally
// and left unexported: types/cellImage.ts models the ephemeral state that
// crosses module boundaries, not one hook's own signature.
interface CellImageDragHandlers {
  onDragEnter: (event: DragEvent<HTMLDivElement>) => void;
  onDragOver: (event: DragEvent<HTMLDivElement>) => void;
  onDragLeave: (event: DragEvent<HTMLDivElement>) => void;
  onDrop: (event: DragEvent<HTMLDivElement>) => void;
}

// What a cell needs in order to render the affordance: the handler set, whether
// an acceptable payload is hovering right now, and whether the last drop on this
// particular cell was refused.
interface UseCellImageDropResult {
  dragHandlers: CellImageDragHandlers;
  isDragActive: boolean;
  isRejecting: boolean;
}

// True when the drag advertises at least one file. Defined at module scope so it
// is built once rather than per render and needs no slot in a dependency array.
//
// The dataTransfer guard is not redundant even though React types the property
// as always present: a synthetically dispatched event, or a user agent that
// withholds the data store, can leave it absent at run time, and a throw inside
// a drag handler would strand the affordance in its active state. Array.from
// covers both a real item list and the plain array a test supplies, because
// DOM.Iterable is in the compiler's lib set.
const dragPayloadHasFile = (event: DragEvent<HTMLDivElement>): boolean => {
  const items = event.dataTransfer ? event.dataTransfer.items : null;
  if (!items) {
    return false;
  }
  return Array.from(items).some((item) => item.kind === 'file');
};

// Makes the cursor tell the truth: a copy affordance when this cell will take
// the payload, and none when it will not. Assigning dropEffect is the only
// channel a page has for that, and it has to be reassigned on every hover event
// because the drag operation is renegotiated each time one fires.
const signalDropEffect = (event: DragEvent<HTMLDivElement>, canAccept: boolean): void => {
  if (event.dataTransfer) {
    event.dataTransfer.dropEffect = canAccept ? 'copy' : 'none';
  }
};

// Supplies one cell's drag handlers. The parameter is deliberately widened to
// include undefined: the cell's image key prop is optional, so the call site
// passes a possibly-absent key and must never be forced into a cast or a
// narrowing check in order to do so. An absent key makes every handler inert,
// which is what lets a cell rendered without one behave exactly as it did before
// this feature existed.
export function useCellImageDrop(key: string | undefined): UseCellImageDropResult {
  // Only the three members this hook actually uses. The store owns validation,
  // the blob URL lifecycle and the notice's lifetime, and none of that is this
  // hook's business. Reading through the context also means the hook behaves
  // identically when no provider is mounted above it, because the context's
  // default value is a working inert implementation rather than undefined.
  const { setCellImage, rejectCellImage, rejection } = useCellImages();

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
  const isRejecting = rejection !== null && key !== undefined && rejection.key === key;

  const onDragEnter = useCallback(
    (event: DragEvent<HTMLDivElement>): void => {
      // Cancelling here as well as on dragover keeps the element a valid drop
      // target from the very first event of the gesture.
      event.preventDefault();

      const canAccept = key !== undefined && dragPayloadHasFile(event);
      signalDropEffect(event, canAccept);
      if (!canAccept) {
        // A cell with no key, or a payload carrying no file, shows no
        // affordance at all: an outline must promise only what a drop will
        // actually honour.
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

  // The parameter is annotated but deliberately unread, and the leading
  // underscore is what records that for the compiler and the reader alike. A
  // leave has no default action worth cancelling, and the payload must not be
  // re-inspected here: a user agent may withhold the item list on the way out,
  // and reading that as "no file" would switch the outline off while the pointer
  // is still inside the cell, which is the very flicker the depth counter exists
  // to remove.
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
        // Inert. Nothing is validated, nothing is stored and nothing is
        // announced, so a cell without a key is left exactly as it was.
        return;
      }

      const files = event.dataTransfer ? Array.from(event.dataTransfer.files) : [];
      if (files.length === 0) {
        // Silent no-op, and the path a cross-page drag takes: it carries URL or
        // markup strings rather than a File. There is nothing to announce
        // because the user did nothing wrong, and no cell is touched.
        return;
      }

      // The first acceptable image file wins and the rest are ignored: fanning
      // out across neighbouring cells would need grid geometry and would change
      // selection semantics. The allow-list is matched with some() because it is
      // a readonly tuple of literal types, so the array's own membership test
      // would accept only those five literals while a file's type is a plain
      // string.
      const accepted = files.find((file) =>
        ACCEPTED_IMAGE_MIME_TYPES.some((mimeType) => mimeType === file.type),
      );

      if (accepted) {
        // The store re-checks the type and enforces the byte ceiling before it
        // allocates anything, so an oversized picture of an accepted type comes
        // back from there as a too-large rejection instead of an image.
        setCellImage(key, accepted);
        return;
      }

      // Files arrived but none was a raster image this feature accepts. Only
      // the notice changes: the cell keeps its value and its formula, so
      // whatever it displayed before the drop still displays afterwards.
      rejectCellImage(key, 'unsupported-type', files[0].name);
    },
    [key, rejectCellImage, setCellImage],
  );

  // Memoized separately from the result so the object a cell spreads onto its
  // root element keeps its identity while the two flags change around it.
  const dragHandlers = useMemo<CellImageDragHandlers>(
    () => ({ onDragEnter, onDragOver, onDragLeave, onDrop }),
    [onDragEnter, onDragOver, onDragLeave, onDrop],
  );

  // A stable identity for the whole result, so a cell re-rendering for an
  // unrelated reason — being typed into, for instance — sees nothing change here
  // and its idle render path stays exactly as it was without this feature.
  return useMemo<UseCellImageDropResult>(
    () => ({ dragHandlers, isDragActive, isRejecting }),
    [dragHandlers, isDragActive, isRejecting],
  );
}
