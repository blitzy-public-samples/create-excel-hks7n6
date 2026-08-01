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
// jsdom implements neither object-URL API, so both are assigned per test: the exactly-one-mint and
// exactly-one-release assertions depend on each test owning its own call counts. They are installed
// at this file's top level rather than inside a describe block because Testing Library's automatic
// unmount runs before this file's teardown, so the provider's release sweep is observed while the
// stubs are still in place.
//
// Ingestion is synchronous, so almost nothing below awaits: a drop is asserted immediately after the
// event that carried it, which is itself the proof that no read, parse, or decode sits between the
// drop and the picture. File contents are arbitrary bytes on purpose — this feature reads a file's
// declared type and length and never its bytes, and a fixture that pretended otherwise would test a
// parser this prototype deliberately does not have.

import { act, createEvent, fireEvent, render, screen, within } from '@testing-library/react';
import { useEffect, useState } from 'react';
import { CellImageProvider, useCellImages } from '../cellImageStore';
import { useCellImageDrop } from '../useCellImageDrop';
import { CellImageOverlay } from '../CellImageOverlay';
import {
  ACCEPTED_IMAGE_MIME_TYPES,
  CELL_IMAGE_TOKENS,
  MAX_IMAGE_BYTES,
} from '../cellImageTokens';
import { cellImageKey } from '../cellImageKey';
import type { CellImageEntry, CellImageRejectionReason } from '../../../types/cellImage';

// Typed against the feature's own union so the assertions cannot drift from the contract: if a
// reason is ever renamed, this file stops compiling instead of silently asserting a dead string.
const UNSUPPORTED_TYPE: CellImageRejectionReason = 'unsupported-type';
const TOO_LARGE: CellImageRejectionReason = 'too-large';

// Deterministic and distinct per mint, which is what lets the replacement case tell the superseded
// URL from its successor.
const mintedUrl = (ordinal: number): string => `blob:cell-image-test/${ordinal}`;

let createObjectUrlSpy: jest.Mock<string, [Blob | MediaSource]>;
let revokeObjectUrlSpy: jest.Mock<void, [string]>;
// The property descriptors as they were before this file touched them, so teardown can put the
// platform back exactly as it found it. jsdom implements neither API, so the descriptor is normally
// absent altogether: assigning the captured value back would leave the property PRESENT with the
// value undefined, and a later suite that asks whether the platform has an object-URL API would get
// the wrong answer. Absent means deleted, present means redefined verbatim.
let createObjectUrlDescriptor: PropertyDescriptor | undefined;
let revokeObjectUrlDescriptor: PropertyDescriptor | undefined;
let urlCounter = 0;

// How many times each harness cell has COMMITTED. Counted from an effect rather than from the render
// body: a render React starts and then discards is not a commit, and counting one would overstate what
// a drop actually costs the grid. No identifier here contains the substring the testing-library lint
// plugin treats as naming a render utility.
const commitTally = new Map<string, number>();

const countCommit = (key: string): void => {
  commitTally.set(key, (commitTally.get(key) ?? 0) + 1);
};

const commitsFor = (key: string): number => commitTally.get(key) ?? 0;

beforeEach(() => {
  urlCounter = 0;
  commitTally.clear();
  createObjectUrlDescriptor = Object.getOwnPropertyDescriptor(URL, 'createObjectURL');
  revokeObjectUrlDescriptor = Object.getOwnPropertyDescriptor(URL, 'revokeObjectURL');
  // Direct assignment rather than jest.spyOn: there is no property on jsdom's URL to spy on, so
  // spyOn would throw before the first test ran.
  createObjectUrlSpy = jest.fn<string, [Blob | MediaSource]>(() => {
    urlCounter += 1;
    return mintedUrl(urlCounter);
  });
  revokeObjectUrlSpy = jest.fn<void, [string]>();
  URL.createObjectURL = createObjectUrlSpy;
  URL.revokeObjectURL = revokeObjectUrlSpy;
});

afterEach(() => {
  if (createObjectUrlDescriptor === undefined) {
    delete (URL as Partial<typeof URL>).createObjectURL;
  } else {
    Object.defineProperty(URL, 'createObjectURL', createObjectUrlDescriptor);
  }
  if (revokeObjectUrlDescriptor === undefined) {
    delete (URL as Partial<typeof URL>).revokeObjectURL;
  } else {
    Object.defineProperty(URL, 'revokeObjectURL', revokeObjectUrlDescriptor);
  }
  // Call counts must never bleed between tests: most cases assert an exact number. Restoring every
  // spy here rather than at the end of the test that installed it means a failing assertion cannot
  // leak a spy on a shared global into the tests that follow.
  jest.clearAllMocks();
  jest.restoreAllMocks();
});

// --- File fixtures ------------------------------------------------------------------------------

const fileOfType = (name: string, type: string): File =>
  new File(['cell-image-fixture-bytes'], name, { type });

const rasterFile = (name: string): File => fileOfType(name, 'image/png');

const textFile = (name: string): File => fileOfType(name, 'text/plain');

const svgFile = (name: string): File => fileOfType(name, 'image/svg+xml');

// size is a read-only accessor on File, so it is redefined rather than assigned, which keeps a
// multi-mebibyte fixture free of an actual multi-mebibyte allocation.
const sizedRasterFile = (name: string, sizeBytes: number): File => {
  const file = rasterFile(name);
  Object.defineProperty(file, 'size', { value: sizeBytes });
  return file;
};

const oversizeRasterFile = (name: string): File => sizedRasterFile(name, MAX_IMAGE_BYTES + 1);

const entryFor = (fileName: string, objectUrl: string): CellImageEntry => ({
  objectUrl,
  fileName,
  mimeType: 'image/png',
  sizeBytes: 1024,
  droppedAt: 0,
});

// Several keys in one worksheet column, so a bulk release can be judged across more than one held URL:
// an implementation that released only the first would satisfy a single-image test.
const keysFor = (count: number, worksheetId: string): string[] => {
  const keys: string[] = [];
  for (let index = 0; index < count; index += 1) {
    keys.push(cellImageKey(worksheetId, index, 0));
  }
  return keys;
};

// Every URL the mint spy handed out, in order, so a bulk release can be checked against the exact set
// that was created rather than against a count alone.
const mintedUrls = (): string[] => createObjectUrlSpy.mock.results.map((result) => result.value);

// Every URL the release spy was handed, in order.
const revokedUrls = (): string[] => revokeObjectUrlSpy.mock.calls.map((call) => call[0]);

// --- Harnesses ---------------------------------------------------------------------------------

interface HarnessCellProps {
  imageKey: string;
  testId?: string;
}

// Stands in for Cell.tsx: one element carrying the drag handler set, a value node the picture
// composites over, and the overlay mounted as a sibling of that value rather than replacing it.
// It subscribes exactly as Cell.tsx does, by key, so the render-scoping assertions below measure the
// real thing. The three probe nodes exist because the provider's own status region announces the
// human-readable message, while the assertions below need the machine-readable reason and the two
// affordance flags.
const HarnessCell = ({ imageKey, testId = 'harness-cell' }: HarnessCellProps) => {
  const { image: entry, clearCellImage, rejection } = useCellImages(imageKey);
  const { dragHandlers, isDragActive, isRejecting } = useCellImageDrop(imageKey);

  // An effect with no dependency list runs after every commit and only after a commit, so this tally
  // counts what React actually put on screen for this cell rather than every render it attempted.
  useEffect(() => {
    countCommit(imageKey);
  });

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

// Several cells under one provider, which is what a per-cell claim has to be judged across: one
// cell's picture and one cell's refusal must both leave every other cell exactly as it was.
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

// Both probes are per-cell subscriptions, so a cell that refused nothing reports neither a reason nor
// an outline: a refusal must carry the right reason for the cell it belongs to AND be invisible
// everywhere else.
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
  const { image: entry, setCellImage, clearCellImage, clearAllCellImages } = useCellImages(imageKey);

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
  const { image: entry, rejection } = useCellImages();
  const { dragHandlers, isDragActive, isRejecting } = useCellImageDrop(undefined);

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

// --- Drop helpers ------------------------------------------------------------------------------

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

// dragover precedes drop, and the same data store instance carries both, because in a real browser
// the drop is never delivered unless the dragover was cancelled first. Nothing is awaited: the whole
// pipeline resolves inside the drop event, and every assertion that follows depends on that.
const dropFiles = (node: HTMLElement, files: File[]): void => {
  const dataTransfer = dataTransferFor(files);
  fireEvent.dragOver(node, { dataTransfer });
  fireEvent.drop(node, { dataTransfer });
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

// The three integration points are read from disk as text instead of being imported. Importing any of
// them would abort this whole suite at load time, because each reaches the store, the router or the
// formatting helper through the repository's pre-existing root-alias prefix, which resolves under no
// alias this project declares. A jest module name mapper would make them importable, but it would also
// hide that inherited resolution failure — the same one that keeps the production build red — so it is
// deliberately not used, and no build or jest configuration is added for these assertions either.
// Reading the source closes the one gap a local harness cannot close: a harness keeps passing when the
// real component's wiring is deleted, and the assertions below do not.
declare const __dirname: string;
declare function require(moduleId: string): unknown;

interface SourceFileSystem {
  readFileSync(path: string, encoding: 'utf8'): string;
}

interface SourcePathResolver {
  resolve(...segments: string[]): string;
}

const sourceFileSystem = require('fs') as SourceFileSystem;
const sourcePathResolver = require('path') as SourcePathResolver;

// This file sits at src/features/cellImages/__tests__, so three levels up is the client's source root.
const CLIENT_SOURCE_ROOT = sourcePathResolver.resolve(__dirname, '..', '..', '..');

const readClientSource = (relativePath: string): string =>
  sourceFileSystem.readFileSync(sourcePathResolver.resolve(CLIENT_SOURCE_ROOT, relativePath), 'utf8');

// Collapsing runs of whitespace lets an assertion name the wiring it cares about without also pinning
// how a formatter happened to wrap it across lines.
const collapseWhitespace = (source: string): string => source.replace(/\s+/g, ' ');

const cellSource = readClientSource('components/Cell.tsx');
const gridSource = readClientSource('components/Grid.tsx');
const appSource = readClientSource('app.tsx');

const cellSourceCollapsed = collapseWhitespace(cellSource);
const gridSourceCollapsed = collapseWhitespace(gridSource);

const occurrences = (source: string, pattern: RegExp): number => source.match(pattern)?.length ?? 0;

// Every module specifier the file imports from, in source order.
const importSources = (source: string): string[] =>
  (source.match(/from '[^']+'/g) ?? []).map((fragment) => fragment.slice("from '".length, -1));

describe('cell image integration seam in components/Cell.tsx', () => {
  it('imports every feature module it consumes through a relative specifier', () => {
    expect(cellSource).toContain("from '../features/cellImages/cellImageStore'");
    expect(cellSource).toContain("from '../features/cellImages/useCellImageDrop'");
    expect(cellSource).toContain("from '../features/cellImages/CellImageOverlay'");
    expect(cellSource).toContain("from '../features/cellImages/cellImageTokens'");
    // The root-alias prefix stays confined to the imports that already used it; reaching the feature
    // that way would not resolve, and would make this component's failure look like the feature's.
    expect(cellSource).not.toMatch(/from '@\/features/);
  });

  it('declares imageKey as an optional prop so no other consumer of Cell is forced to change', () => {
    expect(cellSourceCollapsed).toContain(
      'interface CellProps { id: string; value: any; style: React.CSSProperties; imageKey?: string; }',
    );
  });

  it('addresses the store and the drop hook with that same prop', () => {
    // One key-scoped subscription per cell, so a picture dropped on another cell cannot wake this one.
    // Reading the whole map and indexing it here would cost O(cells) renders per drop.
    expect(cellSourceCollapsed).toContain(
      'const { image: cellImage, clearCellImage } = useCellImages(imageKey);',
    );
    expect(cellSourceCollapsed).toContain(
      'const { dragHandlers, isDragActive, isRejecting } = useCellImageDrop(imageKey);',
    );
    expect(cellSourceCollapsed).toContain('clearCellImage(imageKey);');
    // An unscoped subscription is what the key-scoped one replaced; neither form of it may return.
    expect(cellSourceCollapsed).not.toContain('useCellImages();');
    expect(cellSource).not.toContain('getCellImage');
  });

  it('spreads the drag handlers onto the element that is the cell', () => {
    // Anchored to className="cell" rather than to "a div somewhere in the file", so moving the spread
    // onto a nested wrapper — which would change which box the drop and the affordance belong to —
    // fails here.
    expect(cellSource).toMatch(/<div\s+className="cell"[^>]*\{\.\.\.dragHandlers\}[^>]*>/);
  });

  it('derives the drag affordance from both flags and reads every value from the token module', () => {
    expect(cellSourceCollapsed).toContain(
      'const affordance = isRejecting ? dragRejectOutline : isDragActive ? dragActiveOutline : undefined;',
    );
    expect(cellSourceCollapsed).toContain('outlineWidth: CELL_IMAGE_TOKENS.dropOutlineWidth');
    expect(cellSourceCollapsed).toContain('outlineStyle: CELL_IMAGE_TOKENS.dropOutlineStyle');
    expect(cellSourceCollapsed).toContain('outlineColor: CELL_IMAGE_TOKENS.dropActiveOutlineColor');
    expect(cellSourceCollapsed).toContain('backgroundColor: CELL_IMAGE_TOKENS.dropActiveBackground');
    expect(cellSourceCollapsed).toContain('outlineColor: CELL_IMAGE_TOKENS.dropRejectOutlineColor');
    // No colour, width, inset, z-index or duration literal may appear at a usage site.
    expect(cellSource).not.toMatch(/#[0-9A-Fa-f]{3,8}|rgba?\(|\d+px|\d+ms/);
  });

  it('animates both affordances over the motion token and leaves the idle path untransitioned', () => {
    // The motion token exists for this affordance, so it has to be consumed here or it is a token that
    // documents an intention nothing implements.
    expect(cellSourceCollapsed).toContain(
      'transitionDuration: CELL_IMAGE_TOKENS.transitionDuration,',
    );
    expect(cellSourceCollapsed).toContain('const dragActiveOutline: React.CSSProperties = { ...affordanceTransition,');
    expect(cellSourceCollapsed).toContain('const dragRejectOutline: React.CSSProperties = { ...affordanceTransition,');
    // Exactly the two affordance objects carry it: adding it to the idle path would mean an idle cell
    // no longer forwards the caller's own style object by identity.
    expect(occurrences(cellSource, /\.\.\.affordanceTransition/g)).toBe(2);
  });

  it('establishes the positioned containing block only while a picture is mounted', () => {
    // The overlay pins itself to all four edges. Without this declaration those edges resolve against
    // a higher ancestor and the picture leaves its own cell, which is the layout regression the
    // experiment exists to avoid.
    expect(cellSourceCollapsed).toContain(
      "const cellImageContainingBlock: React.CSSProperties = { position: 'relative' };",
    );
    expect(cellSourceCollapsed).toContain(
      '...(visibleImage !== undefined ? cellImageContainingBlock : undefined)',
    );
    // Applied only through that conditional, so an empty cell never gains a positioned box it did not
    // have before, and no row or column is grown by it.
    expect(occurrences(cellSource, /position: 'relative'/g)).toBe(1);
  });

  it('forwards the caller style object by identity while idle and merges over it otherwise', () => {
    // Identity, not a copy, on the idle path: a cell with no picture and no drag in progress must
    // render exactly as it did before this feature existed. The whole merge is named on the other path
    // so that dropping the affordance spread — which would leave the outline computed but never
    // applied — fails here rather than passing silently.
    expect(cellSourceCollapsed).toContain(
      'visibleImage === undefined && affordance === undefined ? style : { ...style, ...(visibleImage !== undefined ? cellImageContainingBlock : undefined), ...affordance, };',
    );
  });

  it('gates the overlay on the edit state and mounts it from the gated value', () => {
    expect(cellSourceCollapsed).toContain('const visibleImage = isEditing ? undefined : cellImage;');
    expect(cellSourceCollapsed).toContain(
      '{visibleImage !== undefined ? ( <CellImageOverlay entry={visibleImage} onDismiss={handleImageDismiss} /> ) : null}',
    );
    // Mounting from the ungated entry would obstruct the auto-focused editor.
    expect(cellSourceCollapsed).not.toContain('<CellImageOverlay entry={cellImage}');
    expect(occurrences(cellSource, /<CellImageOverlay/g)).toBe(1);
  });

  it('leaves the pre-existing editing behaviour and the keyboard model untouched', () => {
    expect(cellSourceCollapsed).toContain(
      'const handleCellClick = () => { setIsEditing(true); setEditValue(String(value)); };',
    );
    expect(cellSourceCollapsed).toContain(
      'const handleBlur = () => { setIsEditing(false); if (editValue !== String(value)) { dispatch(updateCell({ id, value: editValue })); } };',
    );
    expect(cellSourceCollapsed).toContain(
      "const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => { if (e.key === 'Enter') { handleBlur(); } };",
    );
    expect(cellSourceCollapsed).toContain('<span>{formattedValue}</span>');
    // No tabIndex inside a cell, so the grid's single tab stop stays the keyboard model.
    expect(cellSource).not.toContain('tabIndex');
    // The one pre-existing dispatch, and no second one: a drop, a dismissal and a refusal all leave
    // the cell's value and formula exactly as they were.
    expect(occurrences(cellSource, /dispatch\(/g)).toBe(1);
  });

  it('imports nothing beyond the pre-existing four and the feature four', () => {
    // The whole specifier set is pinned rather than a denylist of specific packages, so any new reach
    // for a browser storage API, a transport client or the persisted schema fails here whatever it is
    // called. It also keeps this file free of the very identifiers the ephemerality scan searches for.
    expect(importSources(cellSource)).toEqual([
      'react',
      '@/store',
      '@/store/workbookSlice',
      '@/utils/cellFormatting',
      '../features/cellImages/cellImageStore',
      '../features/cellImages/useCellImageDrop',
      '../features/cellImages/CellImageOverlay',
      '../features/cellImages/cellImageTokens',
    ]);
  });
});

describe('cell image integration seam in components/Grid.tsx', () => {
  it('derives every cell image key through the feature helper on the row and column basis', () => {
    expect(gridSource).toContain("from '../features/cellImages/cellImageKey'");
    expect(gridSourceCollapsed).toContain(
      'imageKey={cellImageKey(activeWorksheet.id, rowIndex, colIndex)}',
    );
    // Same basis as the React key beside it, which is what keeps a key stable across renders without
    // reading the worksheet's diverging cells collection. Matched as a pattern rather than as a string
    // so the expectation is not itself a template-literal expression.
    expect(gridSource).toMatch(/key=\{`\$\{rowIndex\}-\$\{colIndex\}`\}/);
    expect(occurrences(gridSource, /imageKey=/g)).toBe(1);
  });

  it('keeps the cell as the only drop target and the grid free of feature state', () => {
    expect(gridSource).not.toContain('dragHandlers');
    expect(gridSource).not.toContain('CellImageOverlay');
    expect(gridSource).not.toContain('CellImageProvider');
    expect(gridSource).not.toMatch(/onDrop|onDragOver|onDragEnter|onDragLeave/);
  });

  it('leaves the grid roles and the window arrow-key navigation untouched', () => {
    expect(gridSourceCollapsed).toContain('<div className="grid" role="grid" tabIndex={0}>');
    expect(gridSourceCollapsed).toContain('<div key={rowIndex} className="row" role="row">');
    expect(gridSource).toContain("window.addEventListener('keydown', handleKeyDown");
    expect(gridSource).toContain("window.removeEventListener('keydown', handleKeyDown");
    expect(occurrences(gridSource, /window\.addEventListener/g)).toBe(1);
  });
});

describe('cell image integration seam in app.tsx', () => {
  it('mounts the provider above the router so a client-side route change keeps the images', () => {
    expect(appSource).toContain("from './features/cellImages/cellImageStore'");

    const providerOpen = appSource.indexOf('<CellImageProvider>');
    const routerOpen = appSource.indexOf('<BrowserRouter>');
    const shellOpen = appSource.indexOf('<div className="app-container">');
    const routerClose = appSource.indexOf('</BrowserRouter>');
    const providerClose = appSource.indexOf('</CellImageProvider>');

    expect(providerOpen).toBeGreaterThan(-1);
    // Provider outside the router, router outside the shell: the map outlives a route change and dies
    // only with the document, which is the stated lifetime boundary.
    expect(routerOpen).toBeGreaterThan(providerOpen);
    expect(shellOpen).toBeGreaterThan(routerOpen);
    expect(routerClose).toBeGreaterThan(shellOpen);
    expect(providerClose).toBeGreaterThan(routerClose);

    expect(occurrences(appSource, /<CellImageProvider>/g)).toBe(1);
    expect(occurrences(appSource, /<\/CellImageProvider>/g)).toBe(1);
  });

  it('rides inside the pre-existing provider chain rather than reordering it', () => {
    const reduxOpen = appSource.indexOf('<Provider store={store}>');
    const authOpen = appSource.indexOf('<AuthProvider>');

    expect(reduxOpen).toBeGreaterThan(-1);
    expect(authOpen).toBeGreaterThan(reduxOpen);
    expect(appSource.indexOf('<CellImageProvider>')).toBeGreaterThan(authOpen);
  });
});

describe('cell image drop', () => {
  it('renders one contained image for an accepted raster drop and mints exactly one object URL', () => {
    renderHarness(cellImageKey('ws-1', 0, 0));

    dropFiles(screen.getByTestId('harness-cell'), [rasterFile('photo.png')]);

    const image = screen.getByRole('img');
    // The dropped file's own name is the alternative text, which is the only description available
    // for a picture the application has never seen before.
    expect(image).toHaveAttribute('alt', 'photo.png');
    expect(image).toHaveAttribute('src', mintedUrl(1));
    expect(createObjectUrlSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectUrlSpy).not.toHaveBeenCalled();
    // The value the picture covers is untouched in the document, which is what makes dismissal a
    // pure reveal rather than a restore.
    expect(screen.getByTestId('cell-value')).toHaveTextContent('42');
  });

  it('commits the picture inside the drop event itself, with nothing read or decoded first', () => {
    renderHarness(cellImageKey('ws-1', 0, 9));
    const cell = screen.getByTestId('harness-cell');

    const dataTransfer = dataTransferFor([rasterFile('photo.png')]);
    fireEvent.dragOver(cell, { dataTransfer });
    fireEvent.drop(cell, { dataTransfer });

    // No timer, no microtask, no waitFor: the mint and the paint both belong to the drop's own
    // event, which is the whole point of representing the file with a blob URL instead of reading it.
    expect(createObjectUrlSpy).toHaveBeenCalledTimes(1);
    expect(createObjectUrlSpy).toHaveBeenCalledWith(expect.any(File));
    expect(screen.getByRole('img')).toHaveAttribute('src', mintedUrl(1));
  });

  it('accepts every raster type on the allow-list', () => {
    const keys = ACCEPTED_IMAGE_MIME_TYPES.map((_mimeType, index) => cellImageKey('ws-1', 1, index));
    renderGrid(keys);

    ACCEPTED_IMAGE_MIME_TYPES.forEach((mimeType, index) => {
      const name = `photo-${index}`;
      dropFiles(screen.getByTestId(`cell-${keys[index]}`), [fileOfType(name, mimeType)]);
      expect(screen.getByAltText(name)).toBeInTheDocument();
    });

    expect(screen.getAllByRole('img')).toHaveLength(ACCEPTED_IMAGE_MIME_TYPES.length);
    expect(createObjectUrlSpy).toHaveBeenCalledTimes(ACCEPTED_IMAGE_MIME_TYPES.length);
  });

  it('refuses a non-image file and never mints an object URL for it', () => {
    renderHarness(cellImageKey('ws-1', 2, 0));

    dropFiles(screen.getByTestId('harness-cell'), [textFile('notes.txt')]);

    expect(screen.queryAllByRole('img')).toHaveLength(0);
    // Zero, not one: the type gate runs before anything is allocated, so a refused payload costs
    // nothing at all.
    expect(createObjectUrlSpy).not.toHaveBeenCalled();
    expect(rejectionOf('harness-cell')).toEqual({ reason: UNSUPPORTED_TYPE, rejecting: 'true' });
    expect(screen.getByRole('status')).toHaveTextContent('notes.txt');
  });

  it('refuses a file above the byte ceiling before any object URL is allocated', () => {
    renderHarness(cellImageKey('ws-1', 2, 1));

    dropFiles(screen.getByTestId('harness-cell'), [oversizeRasterFile('huge.png')]);

    expect(screen.queryAllByRole('img')).toHaveLength(0);
    expect(createObjectUrlSpy).not.toHaveBeenCalled();
    expect(rejectionOf('harness-cell')).toEqual({ reason: TOO_LARGE, rejecting: 'true' });
  });

  it('accepts a file exactly at the byte ceiling, so the limit is inclusive', () => {
    renderHarness(cellImageKey('ws-1', 2, 2));

    dropFiles(screen.getByTestId('harness-cell'), [
      sizedRasterFile('at-the-limit.png', MAX_IMAGE_BYTES),
    ]);

    expect(screen.getByAltText('at-the-limit.png')).toBeInTheDocument();
    expect(createObjectUrlSpy).toHaveBeenCalledTimes(1);
  });

  it('dismissing a picture removes it and releases exactly the URL that was minted', () => {
    renderHarness(cellImageKey('ws-1', 3, 0));
    dropFiles(screen.getByTestId('harness-cell'), [rasterFile('photo.png')]);
    expect(createObjectUrlSpy).toHaveBeenCalledTimes(1);

    fireEvent.click(dismissControlFor('photo.png'));

    expect(screen.queryAllByRole('img')).toHaveLength(0);
    expect(revokeObjectUrlSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectUrlSpy).toHaveBeenCalledWith(mintedUrl(1));
    // The cell's own content was never touched, so it is simply visible again.
    expect(screen.getByTestId('cell-value')).toHaveTextContent('42');
  });

  it('replacing a picture on the same cell releases only the superseded URL', () => {
    renderHarness(cellImageKey('ws-1', 3, 1));
    const cell = screen.getByTestId('harness-cell');

    dropFiles(cell, [rasterFile('first.png')]);
    dropFiles(cell, [rasterFile('second.png')]);

    expect(createObjectUrlSpy).toHaveBeenCalledTimes(2);
    expect(revokeObjectUrlSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectUrlSpy).toHaveBeenCalledWith(mintedUrl(1));
    const image = screen.getByRole('img');
    expect(image).toHaveAttribute('alt', 'second.png');
    expect(image).toHaveAttribute('src', mintedUrl(2));
  });

  it('behaves inertly and never throws when no provider is mounted above it', () => {
    // No CellImageProvider and no Redux Provider: the inert context default is what lets a cell be
    // rendered anywhere at all without the feature becoming a prerequisite.
    render(<HarnessCell imageKey={cellImageKey('ws-1', 3, 2)} />);
    const cell = screen.getByTestId('harness-cell');

    expect(() => {
      dropFiles(cell, [rasterFile('photo.png')]);
    }).not.toThrow();

    expect(screen.queryAllByRole('img')).toHaveLength(0);
    expect(screen.queryByRole('status')).toBeNull();
    expect(createObjectUrlSpy).not.toHaveBeenCalled();
    expect(screen.getByTestId('cell-value')).toHaveTextContent('42');
  });
});

describe('cell image ownership under batched mutation', () => {
  const key = cellImageKey('ws-2', 0, 0);

  const renderLifecycle = () =>
    render(
      <CellImageProvider>
        <LifecycleHarness imageKey={key} first={rasterFile('first.png')} second={rasterFile('second.png')} />
      </CellImageProvider>,
    );

  it('releases the superseded URL when the same cell is set twice in one task', () => {
    renderLifecycle();

    fireEvent.click(screen.getByTestId('set-then-set'));

    // Both mints happen, because both files were genuinely accepted; what must not happen is the
    // first URL surviving. Ownership is recorded synchronously, so the second call finds the first.
    expect(createObjectUrlSpy).toHaveBeenCalledTimes(2);
    expect(revokeObjectUrlSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectUrlSpy).toHaveBeenCalledWith(mintedUrl(1));
    expect(screen.getByTestId('entry-url')).toHaveTextContent(mintedUrl(2));
    expect(screen.getByTestId('entry-name')).toHaveTextContent('second.png');
  });

  it('releases the held URL when a cell is set and cleared in one task', () => {
    renderLifecycle();

    fireEvent.click(screen.getByTestId('set-then-clear'));

    expect(createObjectUrlSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectUrlSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectUrlSpy).toHaveBeenCalledWith(mintedUrl(1));
    expect(screen.getByTestId('entry-url')).toHaveTextContent('');
  });

  it('releases the held URL when a cell is set and everything is cleared in one task', () => {
    renderLifecycle();

    fireEvent.click(screen.getByTestId('set-then-clear-all'));

    expect(createObjectUrlSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectUrlSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectUrlSpy).toHaveBeenCalledWith(mintedUrl(1));
    expect(screen.getByTestId('entry-url')).toHaveTextContent('');
  });

  it('does not release the same URL twice when a cell is cleared twice in one task', () => {
    renderLifecycle();
    fireEvent.click(screen.getByTestId('set-first'));
    expect(createObjectUrlSpy).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByTestId('clear-twice'));

    // Ownership is dropped before the URL is released, so the second clear has nothing to find.
    expect(revokeObjectUrlSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectUrlSpy).toHaveBeenCalledWith(mintedUrl(1));
  });

  it('releases every held URL exactly once when the provider unmounts', () => {
    // Three, not one: a sweep that released the first entry and stopped would satisfy a
    // single-image test while leaking everything else the page still held.
    const keys = keysFor(3, 'ws-2-unmount');
    const view = renderGrid(keys);

    keys.forEach((key, index) => {
      dropFiles(screen.getByTestId(`cell-${key}`), [rasterFile(`photo-${index}.png`)]);
    });
    const held = mintedUrls();
    expect(held).toHaveLength(3);
    expect(revokeObjectUrlSpy).not.toHaveBeenCalled();

    view.unmount();

    expect(revokeObjectUrlSpy).toHaveBeenCalledTimes(3);
    expect(revokedUrls().sort()).toEqual([...held].sort());
  });

  it('releases every held URL exactly once when every image is cleared at once', () => {
    const keys = keysFor(3, 'ws-2-clear-all');
    render(
      <CellImageProvider>
        <HarnessGrid imageKeys={keys} />
        <StoreControl label="clear all" onAct={(store) => store.clearAllCellImages()} />
      </CellImageProvider>,
    );
    keys.forEach((key, index) => {
      dropFiles(screen.getByTestId(`cell-${key}`), [rasterFile(`photo-${index}.png`)]);
    });
    const held = mintedUrls();
    expect(held).toHaveLength(3);
    expect(screen.getAllByRole('img')).toHaveLength(3);

    fireEvent.click(screen.getByRole('button', { name: 'clear all' }));

    // Every distinct URL, not merely the first: a bulk release that emptied the map but revoked
    // one entry would still clear the grid, so the released set is compared to the minted set.
    expect(screen.queryAllByRole('img')).toHaveLength(0);
    expect(revokeObjectUrlSpy).toHaveBeenCalledTimes(3);
    expect(revokedUrls().sort()).toEqual([...held].sort());
  });

  it('releases each dismissed picture exactly once and leaves its neighbours held', () => {
    const keys = keysFor(3, 'ws-2-dismiss');
    renderGrid(keys);
    keys.forEach((key, index) => {
      dropFiles(screen.getByTestId(`cell-${key}`), [rasterFile(`photo-${index}.png`)]);
    });

    fireEvent.click(dismissControlFor('photo-1.png'));

    // One dismissal releases one URL — the dismissed cell's own — and the other two stay valid.
    expect(screen.getAllByRole('img')).toHaveLength(2);
    expect(revokeObjectUrlSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectUrlSpy).toHaveBeenCalledWith(mintedUrl(2));

    fireEvent.click(dismissControlFor('photo-0.png'));
    fireEvent.click(dismissControlFor('photo-2.png'));

    expect(screen.queryAllByRole('img')).toHaveLength(0);
    expect(revokeObjectUrlSpy).toHaveBeenCalledTimes(3);
    // Exact order, so a release attributed to the wrong key fails here.
    expect(revokedUrls()).toEqual([mintedUrl(2), mintedUrl(1), mintedUrl(3)]);
  });
});

describe('cell image window guard', () => {
  it('cancels a stray file drop anywhere in the document and leaves other drags alone', () => {
    renderHarness(cellImageKey('ws-3', 0, 0));

    // A file drag that misses every cell: without the guard the browser would navigate away from
    // the application to display the file, ending the experiment mid-assessment.
    expect(dispatchWindowDrag('dragover', [{ kind: 'file', type: 'image/png' }])).toBe(true);
    expect(dispatchWindowDrag('drop', [{ kind: 'file', type: 'image/png' }])).toBe(true);

    // A dragged link or text selection is none of this feature's business, so its default survives.
    expect(dispatchWindowDrag('dragover', [{ kind: 'string', type: 'text/uri-list' }])).toBe(false);
    expect(dispatchWindowDrag('drop', [{ kind: 'string', type: 'text/uri-list' }])).toBe(false);

    // The guard never touches a cell, so a stray drop leaves the grid exactly as it was.
    expect(screen.queryAllByRole('img')).toHaveLength(0);
    expect(createObjectUrlSpy).not.toHaveBeenCalled();
  });

  it('registers exactly the two drag listeners on window and removes exactly those references', () => {
    // Installed immediately before the mount so the recorded calls are the provider's own, and
    // restored from the shared teardown so a failing assertion below cannot leak a spy on a global.
    const addEventListenerSpy = jest.spyOn(window, 'addEventListener');
    const removeEventListenerSpy = jest.spyOn(window, 'removeEventListener');
    const view = renderHarness(cellImageKey('ws-3', 0, 1));

    // The whole registration set, with each listener's options, in order: a third subscription, a
    // capture-phase duplicate or a { once: true } would all fail here, where a containment check
    // would let them through.
    expect(addEventListenerSpy.mock.calls.map((call) => [String(call[0]), call[2]])).toEqual([
      ['dragover', undefined],
      ['drop', undefined],
    ]);
    const registered = addEventListenerSpy.mock.calls.map((call) => call[1]);

    view.unmount();

    // The same handler references are removed, not merely two calls bearing the right event names:
    // removing a different function would leave the real listeners attached to the document forever.
    expect(removeEventListenerSpy.mock.calls.map((call) => [String(call[0]), call[1], call[2]])).toEqual([
      ['dragover', registered[0], undefined],
      ['drop', registered[1], undefined],
    ]);

    // Nothing is cancelled once the provider is gone: the document is left as the browser found it.
    expect(dispatchWindowDrag('drop', [{ kind: 'file', type: 'image/png' }])).toBe(false);
  });
});

describe('cell image editing interaction', () => {
  // A cell in edit mode suppresses the picture entirely, so the auto-focused input is never obstructed
  // by an image. Modelled here because Cell.tsx itself cannot be imported into this suite.
  const EditableHarnessCell = ({ imageKey }: HarnessCellProps) => {
    const { image: entry, clearCellImage } = useCellImages(imageKey);
    const { dragHandlers } = useCellImageDrop(imageKey);
    const [isEditing, setIsEditing] = useState(false);

    return (
      <div data-testid="harness-cell" className="cell" {...dragHandlers}>
        <button type="button" data-testid="begin-edit" onClick={() => setIsEditing(true)}>
          edit
        </button>
        {isEditing ? (
          <input data-testid="cell-input" defaultValue="42" onBlur={() => setIsEditing(false)} />
        ) : (
          <span>42</span>
        )}
        {!isEditing && entry ? (
          <CellImageOverlay entry={entry} onDismiss={() => clearCellImage(imageKey)} />
        ) : null}
      </div>
    );
  };

  it('suppresses the picture while the cell is being edited and restores it afterwards', () => {
    render(
      <CellImageProvider>
        <EditableHarnessCell imageKey={cellImageKey('ws-4', 0, 0)} />
      </CellImageProvider>,
    );

    dropFiles(screen.getByTestId('harness-cell'), [rasterFile('photo.png')]);
    expect(screen.getAllByRole('img')).toHaveLength(1);

    fireEvent.click(screen.getByTestId('begin-edit'));

    expect(screen.getByTestId('cell-input')).toBeInTheDocument();
    expect(screen.queryAllByRole('img')).toHaveLength(0);
    // Suppressing the picture is not releasing it: the URL stays valid so the picture returns intact.
    expect(revokeObjectUrlSpy).not.toHaveBeenCalled();

    // Leaving edit mode is the other half of the claim, and asserting it is what makes the title
    // true: the same picture returns, from the same URL, with nothing minted or released in between.
    fireEvent.blur(screen.getByTestId('cell-input'));

    const restored = screen.getByAltText('photo.png');
    expect(restored).toHaveAttribute('src', mintedUrl(1));
    expect(screen.getAllByRole('img')).toHaveLength(1);
    expect(createObjectUrlSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectUrlSpy).not.toHaveBeenCalled();
  });
});

describe('cell image drag affordance and drop-effect signalling', () => {
  it('cancels dragenter, dragover and drop and advertises a copy effect for a file payload', () => {
    renderHarness(cellImageKey('ws-5', 0, 0));
    const cell = screen.getByTestId('harness-cell');

    const enterTransfer = dataTransferFor([rasterFile('photo.png')]);
    const enterEvent = createEvent.dragEnter(cell, { dataTransfer: enterTransfer });
    fireEvent(cell, enterEvent);
    expect(enterEvent.defaultPrevented).toBe(true);
    expect(enterTransfer.dropEffect).toBe('copy');
    expect(screen.getByTestId('drag-active')).toHaveTextContent('true');

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
    expect(screen.getByTestId('drag-active')).toHaveTextContent('false');
  });

  it('advertises no drop effect and shows no affordance for a payload that carries no file', () => {
    renderHarness(cellImageKey('ws-5', 0, 1));
    const cell = screen.getByTestId('harness-cell');

    const transfer = stringDataTransfer();
    const overEvent = createEvent.dragOver(cell, { dataTransfer: transfer });
    fireEvent(cell, overEvent);

    expect(overEvent.defaultPrevented).toBe(true);
    expect(transfer.dropEffect).toBe('none');
    expect(screen.getByTestId('drag-active')).toHaveTextContent('false');
  });

  it('advertises no drop effect and shows no affordance on dragenter for a payload with no file', () => {
    // The hover phase begins at dragenter, so a cross-page image drag that lit the affordance there
    // would promise a drop this feature then refuses. Covered separately from dragover because the
    // two handlers decide acceptance independently.
    renderHarness(cellImageKey('ws-5', 0, 4));
    const cell = screen.getByTestId('harness-cell');

    const enterTransfer = stringDataTransfer();
    const enterEvent = createEvent.dragEnter(cell, { dataTransfer: enterTransfer });
    fireEvent(cell, enterEvent);

    expect(enterEvent.defaultPrevented).toBe(true);
    expect(enterTransfer.dropEffect).toBe('none');
    expect(screen.getByTestId('drag-active')).toHaveTextContent('false');

    // The leave that follows must not drive the depth counter below zero, or the next real file drag
    // would need two enters before the affordance appeared.
    fireEvent.dragLeave(cell, { dataTransfer: stringDataTransfer() });
    expect(screen.getByTestId('drag-active')).toHaveTextContent('false');

    fireEvent.dragEnter(cell, { dataTransfer: dataTransferFor([rasterFile('photo.png')]) });
    expect(screen.getByTestId('drag-active')).toHaveTextContent('true');
  });

  it('holds the affordance steady across nested enter and leave pairs, clamps at zero and resets on drop', () => {
    renderHarness(cellImageKey('ws-5', 0, 2));
    const cell = screen.getByTestId('harness-cell');
    const transfer = () => ({ dataTransfer: dataTransferFor([rasterFile('photo.png')]) });

    fireEvent.dragEnter(cell, transfer());
    fireEvent.dragEnter(cell, transfer());
    expect(screen.getByTestId('drag-active')).toHaveTextContent('true');

    // Crossing into a child node fires a leave for the node being left; a boolean flag would blink
    // the outline off here, which is why enters are counted against leaves.
    fireEvent.dragLeave(cell, transfer());
    expect(screen.getByTestId('drag-active')).toHaveTextContent('true');

    fireEvent.dragLeave(cell, transfer());
    expect(screen.getByTestId('drag-active')).toHaveTextContent('false');

    // Clamped at zero, so an unmatched leave cannot drive the count negative and strand the
    // affordance on for the rest of the session.
    fireEvent.dragLeave(cell, transfer());
    fireEvent.dragEnter(cell, transfer());
    expect(screen.getByTestId('drag-active')).toHaveTextContent('true');

    dropFiles(cell, [rasterFile('photo.png')]);
    // A drop consumes every outstanding enter, so the count is reset rather than decremented.
    expect(screen.getByTestId('drag-active')).toHaveTextContent('false');
  });

  it('takes the first acceptable image from a multi-file drop and ignores the rest', () => {
    renderHarness(cellImageKey('ws-5', 0, 3));

    dropFiles(screen.getByTestId('harness-cell'), [
      textFile('notes.txt'),
      rasterFile('wanted.png'),
      rasterFile('ignored.png'),
    ]);

    // One picture, not three, and no fan-out into neighbouring cells: that would need grid geometry
    // and would change selection semantics.
    expect(screen.getAllByRole('img')).toHaveLength(1);
    expect(screen.getByAltText('wanted.png')).toBeInTheDocument();
    expect(createObjectUrlSpy).toHaveBeenCalledTimes(1);
  });

  it('treats a drop that carries no file as a silent no-op', () => {
    renderHarness(cellImageKey('ws-5', 0, 4));
    const cell = screen.getByTestId('harness-cell');

    // The shape a cross-page image drag delivers: URL and markup strings, never a File. Honouring
    // it would need a CORS-constrained network fetch, so it is deliberately unsupported.
    const dataTransfer = stringDataTransfer();
    fireEvent.dragOver(cell, { dataTransfer });
    fireEvent.drop(cell, { dataTransfer });

    expect(screen.queryAllByRole('img')).toHaveLength(0);
    expect(createObjectUrlSpy).not.toHaveBeenCalled();
    // Nothing was announced either: the user did nothing wrong, so there is nothing to report.
    expect(screen.queryByRole('status')).toBeNull();
    expect(rejectionOf('harness-cell')).toEqual({ reason: '', rejecting: 'false' });
  });

  it('is completely inert for a cell rendered without an image key', () => {
    render(
      <CellImageProvider>
        <KeylessHarnessCell />
      </CellImageProvider>,
    );
    const cell = screen.getByTestId('keyless-cell');

    const transfer = dataTransferFor([rasterFile('photo.png')]);
    const overEvent = createEvent.dragOver(cell, { dataTransfer: transfer });
    fireEvent(cell, overEvent);
    fireEvent.drop(cell, { dataTransfer: dataTransferFor([rasterFile('photo.png')]) });

    // The default is still cancelled, because a keyless cell must not let the browser navigate
    // away either, but nothing is accepted, allocated, or announced.
    expect(overEvent.defaultPrevented).toBe(true);
    expect(transfer.dropEffect).toBe('none');
    expect(screen.queryByTestId('keyless-entry')).toBeNull();
    expect(screen.getByTestId('keyless-drag-active')).toHaveTextContent('false');
    expect(screen.getByTestId('keyless-rejecting')).toHaveTextContent('false');
    expect(screen.getByTestId('keyless-reason')).toHaveTextContent('');
    expect(createObjectUrlSpy).not.toHaveBeenCalled();
    expect(screen.getByTestId('keyless-value')).toHaveTextContent('42');
  });
});

describe('cell image refusal echo and notice', () => {
  it('refuses an SVG, which the raster-only allow-list deliberately excludes', () => {
    renderHarness(cellImageKey('ws-6', 0, 0));

    dropFiles(screen.getByTestId('harness-cell'), [svgFile('logo.svg')]);

    // Unsanitized SVG is XML that can carry active content, so it is refused rather than rendered.
    expect(screen.queryAllByRole('img')).toHaveLength(0);
    expect(createObjectUrlSpy).not.toHaveBeenCalled();
    expect(rejectionOf('harness-cell')).toEqual({ reason: UNSUPPORTED_TYPE, rejecting: 'true' });
  });

  it('refuses a scriptable format handed straight to the store, not only one that is dropped', () => {
    const key = cellImageKey('ws-6', 0, 1);
    render(
      <CellImageProvider>
        <HarnessCell imageKey={key} />
        <StoreControl label="force svg" onAct={(store) => store.setCellImage(key, svgFile('logo.svg'))} />
      </CellImageProvider>,
    );

    fireEvent.click(screen.getByRole('button', { name: 'force svg' }));

    // The allow-list lives in the store, not only in the drop handler, so bypassing the drag path
    // does not bypass the gate.
    expect(screen.queryAllByRole('img')).toHaveLength(0);
    expect(createObjectUrlSpy).not.toHaveBeenCalled();
    expect(rejectionOf('harness-cell')).toEqual({ reason: UNSUPPORTED_TYPE, rejecting: 'true' });
  });

  it('outlines only the cell whose own drop was refused', () => {
    const keys = [cellImageKey('ws-6', 1, 0), cellImageKey('ws-6', 1, 1)];
    renderGrid(keys);

    dropFiles(screen.getByTestId(`cell-${keys[0]}`), [textFile('notes.txt')]);

    expect(rejectionOf(`cell-${keys[0]}`)).toEqual({
      reason: UNSUPPORTED_TYPE,
      rejecting: 'true',
    });
    // The neighbour refused nothing, so it neither carries an outline nor even observes the reason:
    // its subscription is scoped to its own key.
    expect(rejectionOf(`cell-${keys[1]}`)).toEqual({ reason: '', rejecting: 'false' });
    // One notice belongs to the whole page, never one per cell.
    expect(screen.getAllByRole('status')).toHaveLength(1);
  });

  it('retires the notice for the cell that was corrected and leaves another cell its own', () => {
    const keys = [cellImageKey('ws-6', 2, 0), cellImageKey('ws-6', 2, 1)];
    renderGrid(keys);

    dropFiles(screen.getByTestId(`cell-${keys[0]}`), [textFile('notes.txt')]);
    expect(dragFlagOf(`cell-${keys[0]}`, 'rejecting')).toBe('true');

    // A good drop on a DIFFERENT cell is not this notice's correction, so the complaint stands.
    dropFiles(screen.getByTestId(`cell-${keys[1]}`), [rasterFile('photo.png')]);
    expect(screen.getByAltText('photo.png')).toBeInTheDocument();
    expect(dragFlagOf(`cell-${keys[0]}`, 'rejecting')).toBe('true');
    expect(screen.getByRole('status')).toHaveTextContent('notes.txt');

    // A good drop on the cell that was refused IS the correction, so its notice goes.
    dropFiles(screen.getByTestId(`cell-${keys[0]}`), [rasterFile('replacement.png')]);
    expect(dragFlagOf(`cell-${keys[0]}`, 'rejecting')).toBe('false');
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('retires the notice after its token lifetime and gives a later rejection a full one', () => {
    jest.useFakeTimers();
    try {
      renderHarness(cellImageKey('ws-6', 3, 0));
      const cell = screen.getByTestId('harness-cell');

      dropFiles(cell, [textFile('notes.txt')]);
      expect(screen.getByRole('status')).toBeInTheDocument();

      act(() => {
        jest.advanceTimersByTime(CELL_IMAGE_TOKENS.rejectionNoticeMs - 1);
      });
      expect(screen.getByRole('status')).toBeInTheDocument();

      act(() => {
        jest.advanceTimersByTime(1);
      });
      expect(screen.queryByRole('status')).toBeNull();
      expect(rejectionOf('harness-cell')).toEqual({ reason: '', rejecting: 'false' });
    } finally {
      jest.useRealTimers();
    }
  });

  it('gives a refusal that arrives mid-notice a full lifetime of its own', () => {
    jest.useFakeTimers();
    try {
      renderHarness(cellImageKey('ws-6', 4, 0));
      const cell = screen.getByTestId('harness-cell');
      const halfLife = Math.floor(CELL_IMAGE_TOKENS.rejectionNoticeMs / 2);

      dropFiles(cell, [textFile('first.txt')]);
      expect(screen.getByRole('status')).toHaveTextContent('first.txt');

      // The second refusal arrives while the first notice is still on screen, which is the only
      // arrangement that can tell a reset timer from an inherited one.
      act(() => {
        jest.advanceTimersByTime(halfLife);
      });
      dropFiles(cell, [textFile('second.txt')]);
      expect(screen.getByRole('status')).toHaveTextContent('second.txt');

      // Cross the FIRST notice's original deadline. An implementation that let the first timer run
      // would retire the second notice here, having given it only half a lifetime.
      act(() => {
        jest.advanceTimersByTime(CELL_IMAGE_TOKENS.rejectionNoticeMs - halfLife);
      });
      expect(screen.getByRole('status')).toHaveTextContent('second.txt');

      // The second notice's own remaining lifetime, to one tick short of its deadline and then over it.
      act(() => {
        jest.advanceTimersByTime(halfLife - 1);
      });
      expect(screen.getByRole('status')).toHaveTextContent('second.txt');
      act(() => {
        jest.advanceTimersByTime(1);
      });
      expect(screen.queryByRole('status')).toBeNull();
      expect(rejectionOf('harness-cell')).toEqual({ reason: '', rejecting: 'false' });
    } finally {
      jest.useRealTimers();
    }
  });
});

describe('cell image provider lifetime', () => {
  it('keeps a picture through a child remount and discards it only with the provider', () => {
    const RemountHarness = () => {
      const [mounted, setMounted] = useState<boolean>(true);
      return (
        <CellImageProvider>
          <button type="button" onClick={() => setMounted((previous) => !previous)}>
            toggle cell
          </button>
          {mounted ? <HarnessCell imageKey={cellImageKey('ws-7', 0, 0)} /> : null}
        </CellImageProvider>
      );
    };
    const view = render(<RemountHarness />);

    dropFiles(screen.getByTestId('harness-cell'), [rasterFile('photo.png')]);
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
    renderHarness(cellImageKey('ws-7', 0, 1));

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
  it('releases once, not twice, when one turn clears a cell and unmounts the provider', () => {
    const key = cellImageKey('ws-8', 0, 0);
    render(<UnmountShell imageKey={key} onAct={(store) => store.clearCellImage(key)} />);

    dropFiles(screen.getByTestId('harness-cell'), [rasterFile('photo.png')]);
    expect(createObjectUrlSpy).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: 'act and unmount' }));

    // The URL leaves the live set before it is revoked, so the unmount sweep that follows in the
    // same turn finds nothing left to release.
    expect(revokeObjectUrlSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectUrlSpy).toHaveBeenCalledWith(mintedUrl(1));
  });

  it('releases the superseded and the replacement URL exactly once each when one turn replaces a picture and unmounts', () => {
    const key = cellImageKey('ws-8', 0, 1);
    render(
      <UnmountShell
        imageKey={key}
        onAct={(store) => store.setCellImage(key, rasterFile('second.png'))}
      />,
    );

    dropFiles(screen.getByTestId('harness-cell'), [rasterFile('first.png')]);
    expect(createObjectUrlSpy).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: 'act and unmount' }));

    // Two URLs existed, so two are released: the superseded one by the replacement itself, the
    // replacement by the sweep. Neither is released twice.
    expect(createObjectUrlSpy).toHaveBeenCalledTimes(2);
    expect(revokeObjectUrlSpy).toHaveBeenCalledTimes(2);
    expect(revokeObjectUrlSpy).toHaveBeenNthCalledWith(1, mintedUrl(1));
    expect(revokeObjectUrlSpy).toHaveBeenNthCalledWith(2, mintedUrl(2));
  });
});

describe('cell image render scoping', () => {
  // A spreadsheet renders a cell per visible position, so a drop that woke every mounted cell would
  // cost O(cells) and would show up as grid lag in exactly the visual assessment this feature exists
  // to support. These are the measurements, not assertions of intent.
  const renderTwoCellsAndAControl = (keys: string[], onAct: (store: CellImageStore) => void) =>
    render(
      <CellImageProvider>
        <HarnessCell imageKey={keys[0]} testId={`cell-${keys[0]}`} />
        <HarnessCell imageKey={keys[1]} testId={`cell-${keys[1]}`} />
        <StoreControl label="act" onAct={onAct} />
      </CellImageProvider>,
    );

  it('leaves an untouched neighbouring cell unrendered when another cell receives a picture', () => {
    const keys = [cellImageKey('ws-9', 0, 0), cellImageKey('ws-9', 0, 1)];
    renderTwoCellsAndAControl(keys, () => undefined);

    const neighbourBefore = commitsFor(keys[1]);
    const targetBefore = commitsFor(keys[0]);

    dropFiles(screen.getByTestId(`cell-${keys[0]}`), [rasterFile('photo.png')]);

    // The cell that received the picture renders; the one next to it does not, because its
    // subscription returns the identical snapshot it already held.
    expect(commitsFor(keys[0])).toBeGreaterThan(targetBefore);
    expect(commitsFor(keys[1])).toBe(neighbourBefore);
    expect(screen.getByAltText('photo.png')).toBeInTheDocument();
  });

  it('leaves an untouched neighbouring cell unrendered when another cell is refused', () => {
    const keys = [cellImageKey('ws-9', 1, 0), cellImageKey('ws-9', 1, 1)];
    renderTwoCellsAndAControl(keys, () => undefined);

    const neighbourBefore = commitsFor(keys[1]);

    dropFiles(screen.getByTestId(`cell-${keys[0]}`), [textFile('notes.txt')]);

    // The notice is the provider's own business, so raising one costs the rest of the grid nothing.
    expect(screen.getByRole('status')).toBeInTheDocument();
    expect(commitsFor(keys[1])).toBe(neighbourBefore);
  });

  it('leaves an untouched neighbouring cell unrendered when another cell is dismissed', () => {
    const keys = [cellImageKey('ws-9', 2, 0), cellImageKey('ws-9', 2, 1)];
    renderTwoCellsAndAControl(keys, () => undefined);
    dropFiles(screen.getByTestId(`cell-${keys[0]}`), [rasterFile('photo.png')]);

    const neighbourBefore = commitsFor(keys[1]);
    fireEvent.click(dismissControlFor('photo.png'));

    expect(screen.queryAllByRole('img')).toHaveLength(0);
    expect(commitsFor(keys[1])).toBe(neighbourBefore);
  });

  it('costs no render anywhere when a mutation changes nothing', () => {
    const keys = [cellImageKey('ws-9', 3, 0), cellImageKey('ws-9', 3, 1)];
    const absentKey = cellImageKey('ws-9', 9, 9);
    renderTwoCellsAndAControl(keys, (store) => store.clearCellImage(absentKey));

    dropFiles(screen.getByTestId(`cell-${keys[0]}`), [rasterFile('photo.png')]);
    const beforeNoop = commitsFor(keys[1]);
    const targetBeforeNoop = commitsFor(keys[0]);

    fireEvent.click(screen.getByRole('button', { name: 'act' }));

    // Clearing a key that holds nothing returns the identical state, so the reducer's
    // identity-preserving guards stop the publication before any subscriber is even consulted.
    expect(commitsFor(keys[1])).toBe(beforeNoop);
    expect(commitsFor(keys[0])).toBe(targetBeforeNoop);
  });
});

describe('cell image overlay layer and removal control', () => {
  it('clips the picture inside an inert layer that fills the whole cell box', () => {
    // Rendered in isolation so the layer is the single generic element under the host. Reaching it
    // by role keeps the assertion free of container access and node traversal, both of which this
    // project's lint configuration rejects.
    render(
      <div data-testid="layer-host">
        <CellImageOverlay entry={entryFor('photo.png', 'blob:isolated')} onDismiss={() => undefined} />
      </div>,
    );

    const layer = within(screen.getByTestId('layer-host')).getByRole('generic');
    expect(layer).toHaveStyle({
      position: 'absolute',
      top: '0px',
      right: '0px',
      bottom: '0px',
      left: '0px',
      overflow: 'hidden',
    });
    // Pointer events pass straight through the layer, so a click still reaches the cell root and
    // the drag-depth accounting never sees a spurious enter or leave.
    expect(layer).toHaveStyle({ pointerEvents: 'none' });
    expect(layer).toHaveStyle({ zIndex: String(CELL_IMAGE_TOKENS.overlayZIndex) });
  });

  it('scales the picture inside the cell box instead of resizing the cell', () => {
    renderHarness(cellImageKey('ws-10', 0, 0));
    dropFiles(screen.getByTestId('harness-cell'), [rasterFile('photo.png')]);

    // Contained rather than cropped or stretched, and bounded by the cell it sits in: this is what
    // keeps row height and column width exactly as they were.
    expect(screen.getByRole('img')).toHaveStyle({
      maxWidth: '100%',
      maxHeight: '100%',
      objectFit: 'contain',
      display: 'block',
    });
  });

  it('exposes the picture and a real labelled button that stays clickable inside the inert layer', () => {
    renderHarness(cellImageKey('ws-10', 0, 1));
    dropFiles(screen.getByTestId('harness-cell'), [rasterFile('photo.png')]);

    const dismiss = dismissControlFor('photo.png');
    // A real button, not a styled div: it is reachable by role and name, and it opts back into
    // pointer events that the layer around it refuses.
    expect(dismiss).toHaveAttribute('type', 'button');
    expect(dismiss).toHaveStyle({ pointerEvents: 'auto' });
    expect(dismiss).toHaveStyle({
      inlineSize: CELL_IMAGE_TOKENS.dismissButtonSize,
      blockSize: CELL_IMAGE_TOKENS.dismissButtonSize,
    });
    // Type metrics come from the same token as the box, so the glyph's line box is the
    // control's own size and nothing about this control is a bare literal.
    expect(dismiss).toHaveStyle({
      fontSize: CELL_IMAGE_TOKENS.dismissButtonSize,
      lineHeight: CELL_IMAGE_TOKENS.dismissButtonSize,
    });
  });

  it('calls its dismissal callback exactly once and never activates the cell around it', () => {
    const onDismiss = jest.fn<void, []>();
    const onParentClick = jest.fn<void, []>();
    render(
      // Modelled on Cell.tsx, whose root element opens the inline editor on click: removing a
      // picture must not reach that handler, or every dismissal would start an edit.
      <div data-testid="clickable-cell" className="cell" onClick={onParentClick}>
        <CellImageOverlay entry={entryFor('photo.png', 'blob:isolated')} onDismiss={onDismiss} />
      </div>,
    );

    fireEvent.click(dismissControlFor('photo.png'));

    // Exactly one callback, so the store is asked to release exactly one blob URL. A duplicate call
    // is invisible in the rendered result because a second clear is a no-op, which is precisely why
    // the count is asserted here rather than inferred from the DOM.
    expect(onDismiss).toHaveBeenCalledTimes(1);
    // stopPropagation is observable only as the absence of a parent activation.
    expect(onParentClick).not.toHaveBeenCalled();

    // A click on the picture itself is not on the control, so it reaches the cell root and
    // click-to-edit is preserved.
    fireEvent.click(screen.getByRole('img'));
    expect(onParentClick).toHaveBeenCalledTimes(1);
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it('draws its own hover, pressed and focus treatment as an unclippable inset ring', () => {
    renderHarness(cellImageKey('ws-10', 0, 2));
    dropFiles(screen.getByTestId('harness-cell'), [rasterFile('photo.png')]);
    const dismiss = dismissControlFor('photo.png');

    expect(shadowOf(dismiss)).toBe('none');

    fireEvent.mouseEnter(dismiss);
    expect(shadowOf(dismiss)).toBe(asDeclared('box-shadow', DESTRUCTIVE_RING));

    fireEvent.mouseDown(dismiss);
    expect(shadowOf(dismiss)).toBe(
      asDeclared('box-shadow', `${DESTRUCTIVE_RING}, ${PRESSED_FILL}`),
    );

    fireEvent.mouseUp(dismiss);
    fireEvent.mouseLeave(dismiss);
    expect(shadowOf(dismiss)).toBe('none');

    // Focus outranks pointer state so a keyboard user can still see where they are.
    fireEvent.focus(dismiss);
    expect(shadowOf(dismiss)).toBe(asDeclared('box-shadow', FOCUS_RING));
    fireEvent.keyDown(dismiss, { key: 'Enter' });
    expect(shadowOf(dismiss)).toBe(asDeclared('box-shadow', `${FOCUS_RING}, ${PRESSED_FILL}`));
    fireEvent.keyUp(dismiss, { key: 'Enter' });
    expect(shadowOf(dismiss)).toBe(asDeclared('box-shadow', FOCUS_RING));
    fireEvent.blur(dismiss);
    expect(shadowOf(dismiss)).toBe('none');
  });

  it('does not let a keyboard press on the control remove the picture', () => {
    renderHarness(cellImageKey('ws-10', 0, 3));
    dropFiles(screen.getByTestId('harness-cell'), [rasterFile('photo.png')]);
    const dismiss = dismissControlFor('photo.png');

    // keydown alone is not an activation, so the pressed treatment must not remove anything.
    fireEvent.keyDown(dismiss, { key: 'Enter' });
    fireEvent.keyUp(dismiss, { key: 'Enter' });

    expect(screen.getAllByRole('img')).toHaveLength(1);
    expect(revokeObjectUrlSpy).not.toHaveBeenCalled();

    // The click a browser synthesises from that activation is what removes it.
    fireEvent.click(dismiss);
    expect(screen.queryAllByRole('img')).toHaveLength(0);
    expect(revokeObjectUrlSpy).toHaveBeenCalledTimes(1);
  });
});

describe('cell image design tokens', () => {
  // This repository ships no stylesheet, so the token bag is the whole design system for this
  // feature. Freezing it is what turns an accidental restyle from a component into a runtime
  // error instead of a silent global change, and that property is worth asserting rather than
  // assuming.
  it('is frozen, so no component can restyle the feature by writing to it', () => {
    expect(Object.isFrozen(CELL_IMAGE_TOKENS)).toBe(true);
  });

  it('drives every affordance transition from the one motion token', () => {
    renderHarness(cellImageKey('ws-11', 0, 0));
    dropFiles(screen.getByTestId('harness-cell'), [rasterFile('photo.png')]);

    // The removal control's own state treatment animates over the token duration. The cell's
    // drag affordance reads the same token; that one lives in Cell.tsx, which this suite cannot
    // import because of the repository's unresolved '@/' specifiers, so it is verified by
    // inspection instead.
    expect(dismissControlFor('photo.png')).toHaveStyle({
      transitionDuration: CELL_IMAGE_TOKENS.transitionDuration,
    });
  });
});

describe('cell image addressing', () => {
  it('derives a distinct key for every worksheet, row and column', () => {
    expect(cellImageKey('ws-1', 0, 0)).toBe('ws-1:0:0');
    expect(cellImageKey('ws-1', 1, 0)).not.toBe(cellImageKey('ws-1', 0, 1));
    expect(cellImageKey('ws-2', 0, 0)).not.toBe(cellImageKey('ws-1', 0, 0));
    // Tolerating an absent worksheet id is what keeps the Grid call site cast-free, where the
    // active worksheet is already implicitly typed any.
    expect(cellImageKey(undefined, 2, 3)).toBe('ws:2:3');
    // An empty string is a worksheet id, not an absent one. This distinguishes the required nullish
    // fallback from a logical-or fallback, which would silently rewrite '' to 'ws' and collide the
    // keys of two different worksheets.
    expect(cellImageKey('', 1, 2)).toBe(':1:2');
  });

  it('keeps a picture addressed by its own key while a neighbour stays empty', () => {
    const keys = [cellImageKey('ws-11', 0, 0), cellImageKey('ws-11', 0, 1)];
    renderGrid(keys);

    dropFiles(screen.getByTestId(`cell-${keys[0]}`), [rasterFile('photo.png')]);

    expect(within(screen.getByTestId(`cell-${keys[0]}`)).getByRole('img')).toBeInTheDocument();
    expect(within(screen.getByTestId(`cell-${keys[1]}`)).queryByRole('img')).toBeNull();
  });
});
