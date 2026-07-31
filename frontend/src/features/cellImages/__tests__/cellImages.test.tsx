// The repository's first frontend test suite, and the designated primary
// verification vehicle for the cell-image drop experiment.
//
// WHY THIS SUITE CARRIES THE FEATURE'S PROOF. The client cannot boot as
// delivered: every pre-existing source file imports through a bare alias prefix
// that no declared path mapping resolves, so the production build fails and the
// manual visual assessment the experiment was commissioned for is blocked by
// defects that predate this work and whose repair is out of scope. Everything this
// feature claims is therefore proven here or nowhere.
//
// WHY NOTHING FROM components/ IS IMPORTED. Cell.tsx and Grid.tsx reach the store
// barrel, the workbook slice, the cell-formatting helper and the components barrel
// through that same unresolvable prefix, and the last of those barrels does not
// even exist. Jest is configured with no module-name mapping for the prefix, so
// importing either component would throw "Cannot find module" while this suite was
// still loading and NOT ONE TEST BELOW WOULD RUN. This file therefore stands up its
// own minimal harness that consumes the feature exactly as Cell.tsx does — the same
// two hooks, the same overlay, the same sibling placement — while importing only
// the feature's own modules, by relative path.
//
// WHY NO REDUX PROVIDER APPEARS ANYWHERE BELOW. That absence is the structural
// proof that the ephemeral image state is independent of the persisted workbook.
// If a dropped image reached the store, or a reducer had to be registered for the
// feature to work, this suite could not pass without a store in the tree. It has
// none, deliberately: nothing here imports the Redux bindings, a store module, a
// slice, a selector or a dispatch, and nothing writes to browser storage, to a
// network, or to a persisted document of any kind.
//
// WHY THE OBJECT-URL APIS ARE STUBBED HERE RATHER THAN GLOBALLY. jsdom implements
// neither URL.createObjectURL nor URL.revokeObjectURL, so both have to be
// supplied. Supplying them here — a fresh pair of mocks per test — is what turns
// the store's one-release-per-mint invariant into something directly assertable:
// "minted exactly once", "never minted", and "released exactly once, with the
// superseded URL" are all statements about call counts on mocks this file owns.
// A stub installed once in src/setupTests.ts would share those counts across
// suites and would silently weaken every one of them. jest.spyOn cannot be used
// either, because there is no existing property to spy on, so direct assignment
// paired with an explicit restore is the only correct mechanism.
//
// WHY EVERY DROP IS PRECEDED BY A DRAGOVER. A browser delivers a drop only to an
// element that cancelled the dragover before it, so firing the pair in that order
// models the real gesture instead of taking a shortcut through it.

import { fireEvent, render, screen } from '@testing-library/react';
import type { RenderResult } from '@testing-library/react';
import { CellImageOverlay } from '../CellImageOverlay';
import { cellImageKey } from '../cellImageKey';
import { CellImageProvider, useCellImages } from '../cellImageStore';
import { MAX_IMAGE_BYTES } from '../cellImageTokens';
import { useCellImageDrop } from '../useCellImageDrop';
import type { CellImageRejectionReason } from '../../../types/cellImage';

// The values captured before each test and put back after it. The declared types
// are the DOM's own; at run time jsdom leaves both APIs undefined, so what is
// captured — and therefore what is restored — is undefined. Restoring that is
// exactly right: it returns the environment to not implementing them, so no stub
// of this suite's can leak into another one.
let originalCreateObjectURL: typeof URL.createObjectURL;
let originalRevokeObjectURL: typeof URL.revokeObjectURL;

// Recreated for every test so call counts can never bleed between cases, which
// matters because five of the six assert exact counts. Create React App's Jest
// configuration also enables resetMocks, and its reset hook is registered before
// this file is even loaded, so it runs ahead of the beforeEach below and cannot
// wipe the implementations installed there.
let createObjectUrl: jest.Mock<string, [Blob | MediaSource]>;
let revokeObjectUrl: jest.Mock<void, [string]>;

// Counts the mints made during one test, so each returns a different URL.
let mintedUrlCount = 0;

// The deterministic URL the stub returns for the nth mint of a test. Distinct per
// call by construction, which is the whole reason "the superseded URL, and only
// that one, was released" can be asserted at all.
const mockObjectUrl = (mintIndex: number): string => `blob:cell-image-test/${mintIndex}`;

beforeEach(() => {
  mintedUrlCount = 0;
  originalCreateObjectURL = URL.createObjectURL;
  originalRevokeObjectURL = URL.revokeObjectURL;
  createObjectUrl = jest.fn<string, [Blob | MediaSource]>(() => {
    mintedUrlCount += 1;
    return mockObjectUrl(mintedUrlCount);
  });
  revokeObjectUrl = jest.fn<void, [string]>();
  // Direct assignment rather than jest.spyOn: jsdom defines no property to spy
  // on, so spyOn would throw before a single assertion ran.
  URL.createObjectURL = createObjectUrl;
  URL.revokeObjectURL = revokeObjectUrl;
});

afterEach(() => {
  // Testing Library's own cleanup is registered when it is imported, so it
  // unmounts the tree before this hook runs. That ordering matters: the
  // provider's unmount sweep releases whatever object URLs are still held, and it
  // has to find the stub still installed when it does.
  URL.createObjectURL = originalCreateObjectURL;
  URL.revokeObjectURL = originalRevokeObjectURL;
  jest.clearAllMocks();
});

// Real File instances rather than stand-ins: the store reads name, type and size
// straight off the dropped file, so anything less would be testing a fiction.
const imageFile = (fileName: string, mimeType: string): File =>
  new File(['raster-bytes'], fileName, { type: mimeType });

const textFile = (fileName: string): File =>
  new File(['plain text, not an image'], fileName, { type: 'text/plain' });

// Crosses the byte ceiling by redefining size rather than by allocating ten real
// mebibytes: size is a read-only accessor on File, and what is under test is the
// comparison, not the allocation.
const oversizeImageFile = (fileName: string): File => {
  const file = imageFile(fileName, 'image/png');
  Object.defineProperty(file, 'size', { value: MAX_IMAGE_BYTES + 1 });
  return file;
};

// The shape a real drag delivers, reduced to the three members the feature reads:
// the file list a drop hands over, the item kinds hover-phase acceptance is
// decided from, and the advertised types. jsdom implements no DataTransfer, so
// Testing Library attaches this plain object to the event verbatim, which makes it
// both necessary and sufficient. Declared as a named type so the value handed to
// fireEvent is not a fresh literal, and no excess-property check applies to it.
interface DragPayloadInit {
  dataTransfer: {
    files: File[];
    items: Array<{ kind: string; type: string }>;
    types: string[];
  };
}

const dragPayload = (files: File[]): DragPayloadInit => ({
  dataTransfer: {
    files,
    items: files.map((file) => ({ kind: 'file', type: file.type })),
    types: ['Files'],
  },
});

// Fires the gesture the way a browser does: the drop is delivered only because the
// dragover before it was cancelled. A fresh payload per event mirrors a real drag,
// in which each event carries its own data-transfer object.
const dropFiles = (node: HTMLElement, files: File[]): void => {
  fireEvent.dragOver(node, dragPayload(files));
  fireEvent.drop(node, dragPayload(files));
};

// Declared locally and left unexported: src/types/cellImage.ts models the
// ephemeral state that crosses module boundaries, not this harness's signature.
interface HarnessCellProps {
  imageKey: string;
}

// Stands in for Cell.tsx without importing it, and consumes the feature exactly as
// Cell.tsx does: the same two hooks, the handler set spread onto the cell root,
// and the overlay mounted as a SIBLING of the value node rather than in place of
// it — which is what lets these tests show that a dropped picture layers over a
// cell's value instead of replacing it.
//
// The three extra spans exist because the two drag flags and the pending
// rejection's machine-readable reason are otherwise unobservable from the DOM: the
// provider's live region announces the human-readable message, and the message's
// wording is not part of any contract, while the reason is.
const HarnessCell = ({ imageKey }: HarnessCellProps): JSX.Element => {
  const { getCellImage, clearCellImage, rejection } = useCellImages();
  const { dragHandlers, isDragActive, isRejecting } = useCellImageDrop(imageKey);
  const entry = getCellImage(imageKey);

  return (
    <div data-testid="harness-cell" className="cell" {...dragHandlers}>
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

// The feature's own provider is the only wrapper. No Redux store, no router, no
// theme: if the feature needed any of them, this render would fail outright.
const renderHarness = (imageKey: string): RenderResult =>
  render(
    <CellImageProvider>
      <HarnessCell imageKey={imageKey} />
    </CellImageProvider>,
  );

const harnessCell = (): HTMLElement => screen.getByTestId('harness-cell');

// Asserts the machine-readable reason rather than the notice's prose. Typing the
// parameter as the union makes a misspelled reason a compile error here instead of
// a test that passes by comparing two equally wrong strings.
const expectRejection = (reason: CellImageRejectionReason): void => {
  expect(screen.getByTestId('rejection-reason').textContent).toBe(reason);
  expect(screen.getByRole('status')).toBeInTheDocument();
};

// The empty string is compared directly rather than through toHaveTextContent,
// which deliberately refuses an empty expectation.
const expectNoRejection = (): void => {
  expect(screen.getByTestId('rejection-reason').textContent).toBe('');
  expect(screen.queryByRole('status')).toBeNull();
};

// "No picture" is asserted through the two nodes the overlay is the sole source of
// in this harness: the picture itself and its removal control. Checking both proves
// the overlay never mounted, rather than merely that its picture went unnamed. Both
// queries run against the whole document rather than a container element, so an
// image escaping into a portal would still be caught.
const expectNoImage = (): void => {
  expect(screen.queryAllByRole('img')).toHaveLength(0);
  expect(screen.queryByRole('button')).toBeNull();
};

describe('cell image drop', () => {
  it('renders a dropped raster image inside the cell it was dropped on', () => {
    renderHarness(cellImageKey('ws-1', 0, 0));

    dropFiles(harnessCell(), [imageFile('photo.png', 'image/png')]);

    // Exactly one image, and it is the one that was dropped: the alternative text
    // carries the file name so assistive technology can name the picture, and the
    // source is the blob URL the store minted for it.
    expect(screen.getAllByRole('img')).toHaveLength(1);
    const image = screen.getByRole('img');
    expect(image).toHaveAttribute('alt', 'photo.png');
    expect(image).toHaveAttribute('src', mockObjectUrl(1));

    // One mint and no release: the URL has to stay valid for as long as the
    // picture is on screen, which is why this design does not release it as soon
    // as the image has loaded.
    expect(createObjectUrl).toHaveBeenCalledTimes(1);
    expect(revokeObjectUrl).not.toHaveBeenCalled();

    // The cell's own value is untouched underneath the picture, and an accepted
    // drop announces nothing.
    expect(screen.getByTestId('cell-value')).toHaveTextContent('42');
    expectNoRejection();
  });

  it('refuses a payload that is not an image and mints no object URL at all', () => {
    renderHarness(cellImageKey('ws-1', 1, 2));

    dropFiles(harnessCell(), [textFile('notes.txt')]);

    expectNoImage();
    expectRejection('unsupported-type');

    // The load-bearing assertion of the feature's security posture: validation
    // runs BEFORE anything is allocated, so a refused payload never receives a
    // blob URL in the first place and no unreleasable resource can exist.
    expect(createObjectUrl).not.toHaveBeenCalled();
    expect(revokeObjectUrl).not.toHaveBeenCalled();

    // The refusing cell shows it, and the cell keeps the value it had before.
    expect(screen.getByTestId('rejecting')).toHaveTextContent('true');
    expect(screen.getByTestId('cell-value')).toHaveTextContent('42');
  });

  it('refuses a file above the byte ceiling even though its type is accepted', () => {
    renderHarness(cellImageKey('ws-1', 3, 4));

    // An accepted MIME type is essential here. The hook's allow-list filter runs
    // first, so an unsupported type would be turned away before the size gate was
    // ever reached and this case would quietly prove the wrong thing.
    dropFiles(harnessCell(), [oversizeImageFile('enormous.png')]);

    expectNoImage();
    expectRejection('too-large');

    // The ceiling is checked before the mint, so an oversized blob is never
    // retained even momentarily.
    expect(createObjectUrl).not.toHaveBeenCalled();
    expect(screen.getByTestId('cell-value')).toHaveTextContent('42');
  });

  it('removes the picture and releases its object URL exactly once when dismissed', () => {
    renderHarness(cellImageKey('ws-1', 5, 6));

    dropFiles(harnessCell(), [imageFile('photo.png', 'image/png')]);
    const mintedUrl = screen.getByRole('img').getAttribute('src');
    expect(mintedUrl).toBe(mockObjectUrl(1));

    // The removal control is a real button whose accessible name identifies the
    // file, so it is located here the way a user of assistive technology would
    // find it rather than by reaching for a class name or a test id.
    fireEvent.click(screen.getByRole('button', { name: /photo\.png/ }));

    // The whole overlay went away with the picture, control included.
    expectNoImage();

    // Release path (b) of the store's one-release-per-mint invariant: exactly one
    // release, naming the URL that was minted for this cell.
    expect(revokeObjectUrl).toHaveBeenCalledTimes(1);
    expect(revokeObjectUrl).toHaveBeenCalledWith(mintedUrl);

    // Dismissing reveals the untouched value again and mints no replacement.
    expect(screen.getByTestId('cell-value')).toHaveTextContent('42');
    expect(createObjectUrl).toHaveBeenCalledTimes(1);
  });

  it('releases only the superseded URL when a second image replaces the first', () => {
    renderHarness(cellImageKey('ws-1', 7, 8));

    dropFiles(harnessCell(), [imageFile('first.png', 'image/png')]);
    const supersededUrl = screen.getByRole('img').getAttribute('src');

    dropFiles(harnessCell(), [imageFile('second.jpg', 'image/jpeg')]);
    const currentUrl = screen.getByRole('img').getAttribute('src');

    // Two mints, and the two URLs differ, which is what makes the question "was
    // the right one released?" answerable rather than merely plausible.
    expect(createObjectUrl).toHaveBeenCalledTimes(2);
    expect(supersededUrl).toBe(mockObjectUrl(1));
    expect(currentUrl).toBe(mockObjectUrl(2));
    expect(currentUrl).not.toBe(supersededUrl);

    // Release path (a): one release, naming the replaced picture. The URL now on
    // screen must never be released while it is still being rendered.
    expect(revokeObjectUrl).toHaveBeenCalledTimes(1);
    expect(revokeObjectUrl).toHaveBeenCalledWith(supersededUrl);
    expect(revokeObjectUrl).not.toHaveBeenCalledWith(currentUrl);

    // The cell shows the replacement, one image and no more, and a successful drop
    // leaves no notice behind it.
    expect(screen.getAllByRole('img')).toHaveLength(1);
    expect(screen.getByRole('img')).toHaveAttribute('alt', 'second.jpg');
    expectNoRejection();
  });

  it('stays completely inert when no provider is mounted above the cell', () => {
    // Rendered bare: no CellImageProvider, and no Redux store either. The context
    // ships a working inert default precisely so a cell can be rendered in
    // isolation, which is what keeps mounting the provider from becoming a hard
    // prerequisite for any page or test that renders a cell. The key is derived
    // without a worksheet id, exercising that fallback of the key function too.
    render(<HarnessCell imageKey={cellImageKey(undefined, 9, 9)} />);

    dropFiles(harnessCell(), [imageFile('photo.png', 'image/png')]);

    expectNoImage();
    expect(createObjectUrl).not.toHaveBeenCalled();
    expect(revokeObjectUrl).not.toHaveBeenCalled();
    expectNoRejection();

    // Nothing about the cell changed, and no flag was left stuck on by the
    // gesture that just passed through it.
    expect(screen.getByTestId('cell-value')).toHaveTextContent('42');
    expect(screen.getByTestId('drag-active')).toHaveTextContent('false');
    expect(screen.getByTestId('rejecting')).toHaveTextContent('false');
  });
});
