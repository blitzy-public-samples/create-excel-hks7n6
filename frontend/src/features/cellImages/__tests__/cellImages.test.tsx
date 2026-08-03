// jsdom lacks object-URL APIs, so each test installs and restores deterministic stubs. Cell/Grid/App
// cannot be imported here while the repository's existing root-alias import specifiers are unresolved;
// local harnesses exercise feature modules, while source-text assertions pin those integration seams.
// One behaviour cannot be reached that way at all — what React's update path does to a caller's style
// shorthand when the affordance withdraws — so cellStyleShorthand.test.tsx alongside this file renders
// the real Cell against per-file stand-ins for exactly those three specifiers.

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

const mintedUrl = (ordinal: number): string => `blob:cell-image-test/${ordinal}`;

let createObjectUrlSpy: jest.Mock<string, [Blob | MediaSource]>;
let revokeObjectUrlSpy: jest.Mock<void, [string]>;
// Restore original descriptors so object-URL APIs that are absent in jsdom remain absent after the
// test.
let createObjectUrlDescriptor: PropertyDescriptor | undefined;
let revokeObjectUrlDescriptor: PropertyDescriptor | undefined;
let urlCounter = 0;

// Count committed renders from an effect; a render-body counter would include renders React abandons.
// The identifier avoids Testing Library's render-helper naming heuristic.
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
  // Restore shared-global spies here so a failed test cannot leak them into the next case.
  jest.clearAllMocks();
  jest.restoreAllMocks();
});

// --- File fixtures ------------------------------------------------------------------------------

const fileOfType = (name: string, type: string): File =>
  new File(['cell-image-fixture-bytes'], name, { type });

const rasterFile = (name: string): File => fileOfType(name, 'image/png');

// Genuine 1×1 PNG/GIF fixtures verify the accepted path with real raster bytes; most gate tests use
// arbitrary bytes because production validates only File.type and File.size. The PNG below is
// 69 bytes.
const MINIMAL_PNG_BYTES = new Uint8Array([
  137, 80, 78, 71, 13, 10, 26, 10, 0, 0, 0, 13, 73, 72, 68, 82, 0, 0, 0, 1, 0, 0, 0, 1, 8, 2, 0, 0,
  0, 144, 119, 83, 222, 0, 0, 0, 12, 73, 68, 65, 84, 120, 218, 99, 248, 207, 192, 0, 0, 3, 1, 1, 0,
  247, 3, 65, 67, 0, 0, 0, 0, 73, 69, 78, 68, 174, 66, 96, 130,
]);

// GIF89a: header, a 1x1 logical screen with a two-entry global colour table, a graphic control
// extension, one image descriptor and a single LZW-coded pixel, then the trailer. 43 bytes.
const MINIMAL_GIF_BYTES = new Uint8Array([
  71, 73, 70, 56, 57, 97, 1, 0, 1, 0, 128, 0, 0, 255, 0, 0, 0, 0, 0, 33, 249, 4, 0, 0, 0, 0, 0, 44,
  0, 0, 0, 0, 1, 0, 1, 0, 0, 2, 2, 68, 1, 0, 59,
]);

const realRasterFile = (name: string, bytes: Uint8Array, type: string): File =>
  new File([bytes], name, { type });

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

// A file that declares an accepted raster type and carries nothing: the one thing a
// metadata-only gate can tell about a payload's content without reading a byte.
const emptyRasterFile = (name: string): File => new File([], name, { type: 'image/png' });

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

const revokedUrls = (): string[] => revokeObjectUrlSpy.mock.calls.map((call) => call[0]);

// --- Harnesses ---------------------------------------------------------------------------------

interface HarnessCellProps {
  imageKey: string;
  testId?: string;
}

// Models Cell's key-scoped hooks and sibling overlay/value composition; probe nodes expose
// machine-readable state that the provider's human-facing status region does not.
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
const DESTRUCTIVE_RING = `inset 0 0 0 ${CELL_IMAGE_TOKENS.dropOutlineWidth} ${CELL_IMAGE_TOKENS.dropRejectOutlineColor}`;
const PRESSED_FILL = `inset 0 0 0 ${CELL_IMAGE_TOKENS.dismissButtonSize} ${CELL_IMAGE_TOKENS.dropActiveBackground}`;

const shadowOf = (node: HTMLElement): string => node.style.getPropertyValue('box-shadow');

// The wording the feature falls back to when a payload carries no file name, spelled out here rather
// than imported so these cases pin the copy a user actually hears instead of restating the source.
const NEUTRAL_FILE_LABEL = 'the dropped file';

const dismissControlFor = (fileName: string): HTMLElement =>
  screen.getByRole('button', { name: `Remove image ${fileName}` });

const dragFlagOf = (cellTestId: string, flagTestId: string): string =>
  within(screen.getByTestId(cellTestId)).getByTestId(flagTestId).textContent ?? '';

// Use one DataTransfer for dragover and drop because cancelling dragover is what allows the browser
// to deliver drop; the production path is synchronous.
const dropFiles = (node: HTMLElement, files: File[]): void => {
  const dataTransfer = dataTransferFor(files);
  fireEvent.dragOver(node, { dataTransfer });
  fireEvent.drop(node, { dataTransfer });
};

// jsdom implements no DragEvent, so Testing Library falls back to a plain Event and a relatedTarget in
// the init is dropped — the same gap it papers over for DataTransfer. Defining the destination on the
// event is therefore the only way to model a user agent that reports where a drag moved to; omitting
// it, as every other leave in this file does, models one that does not.
const dragLeaveTowards = (node: HTMLElement, destination: HTMLElement, files: File[]): void => {
  const event = createEvent.dragLeave(node, { dataTransfer: dataTransferFor(files) });
  Object.defineProperty(event, 'relatedTarget', { value: destination });
  fireEvent(node, event);
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

// --- Prohibited-sink identifiers ------------------------------------------------------------------

// The source scan includes this test directory, so forbidden identifiers are assembled from fragments
// to prevent instrumentation text from matching itself. A later test reassembles each identifier
// independently to catch typos.
const identifierFrom = (...fragments: string[]): string => fragments.join('');

const SINK_TEXT = {
  networkFetch: identifierFrom('fet', 'ch'),
  localStore: identifierFrom('local', 'Storage'),
  sessionStore: identifierFrom('session', 'Storage'),
  indexedDatabase: identifierFrom('indexed', 'DB'),
  httpClientPackage: identifierFrom('ax', 'ios'),
  backendSdkPackage: identifierFrom('fire', 'base'),
} as const;

// The import prefix every pre-existing module in this repository reaches through and that no declared
// alias resolves — the direct cause of most of the inherited unresolved-module diagnostics. Assembled
// for the same reason as the identifiers above, since the scan searches for it too.
const ROOT_ALIAS = identifierFrom('@', '/');

// Reaching a sink by assembled name needs one indexed access, which the DOM typings cannot narrow.
// The assertions go through `unknown` rather than a suppression: nothing is silenced, the shape is
// stated, and a wrong name throws at call time instead of quietly recording nothing — which is the
// behaviour a tripwire probe should have.
type SinkCall = (...args: unknown[]) => unknown;

const windowSlot = (name: string): unknown => (window as unknown as Record<string, unknown>)[name];

const callWindowSink = (name: string, ...args: unknown[]): void => {
  (windowSlot(name) as SinkCall)(...args);
};

// Called as a member of its own namespace so the receiver is what a real call site would pass, which
// is what makes a counter installed on a shared prototype record it.
const callWindowSinkMethod = (name: string, method: string, ...args: unknown[]): void => {
  const namespace = windowSlot(name) as Record<string, SinkCall>;
  namespace[method](...args);
};

// --- Prohibited-sink tripwires ------------------------------------------------------------------

// Instrument the ten transport/storage APIs this suite promises to watch. Preserve descriptors so
// jsdom APIs that were absent are deleted again during teardown.
interface SinkTripwire {
  name: string;
  calls: () => number;
  restore: () => void;
}

const installSinkTripwire = (
  host: object,
  property: string,
  label: string,
  behaviour: 'method' | 'setter' | 'namespace' = 'method',
): SinkTripwire => {
  const original = Object.getOwnPropertyDescriptor(host, property);
  const spy = jest.fn();
  // A namespace sink is reached through one of its own methods rather than by being called, so the
  // counter is installed on each of them; a bare function would throw on the property access instead
  // of recording the attempt, and a throw proves less than a count.
  const namespace = { open: spy, deleteDatabase: spy, databases: spy };
  const descriptor: PropertyDescriptor =
    behaviour === 'setter'
      ? { configurable: true, get: () => '', set: spy }
      : { configurable: true, writable: true, value: behaviour === 'namespace' ? namespace : spy };

  Object.defineProperty(host, property, descriptor);

  return {
    name: label,
    calls: () => spy.mock.calls.length,
    restore: () => {
      if (original === undefined) {
        delete (host as Record<string, unknown>)[property];
      } else {
        Object.defineProperty(host, property, original);
      }
    },
  };
};

// The complete set, named so a failure says which boundary was crossed rather than only that one was.
// Storage and XHR are instrumented on their prototypes because jsdom hands out instances whose own
// properties cannot be redefined.
const installProhibitedSinkTripwires = (): SinkTripwire[] => [
  installSinkTripwire(window, SINK_TEXT.networkFetch, SINK_TEXT.networkFetch),
  installSinkTripwire(XMLHttpRequest.prototype, 'open', 'XMLHttpRequest.open'),
  installSinkTripwire(XMLHttpRequest.prototype, 'send', 'XMLHttpRequest.send'),
  installSinkTripwire(navigator, 'sendBeacon', 'navigator.sendBeacon'),
  installSinkTripwire(Storage.prototype, 'setItem', 'Storage.setItem'),
  installSinkTripwire(Storage.prototype, 'getItem', 'Storage.getItem'),
  installSinkTripwire(Storage.prototype, 'removeItem', 'Storage.removeItem'),
  installSinkTripwire(Storage.prototype, 'clear', 'Storage.clear'),
  installSinkTripwire(
    window,
    SINK_TEXT.indexedDatabase,
    `${SINK_TEXT.indexedDatabase}.open`,
    'namespace',
  ),
  installSinkTripwire(document, 'cookie', 'document.cookie', 'setter'),
];

const crossedSinks = (tripwires: SinkTripwire[]): string[] =>
  tripwires.filter((tripwire) => tripwire.calls() > 0).map((tripwire) => tripwire.name);

// --- Feature source policy -----------------------------------------------------------------------

// Every production module of the feature. Enumerated rather than globbed so that adding a module
// without adding it here is itself visible: the completeness assertion below checks this list against
// what the directory actually contains.
const FEATURE_SOURCE_PATHS = [
  'features/cellImages/cellImageStore.tsx',
  'features/cellImages/useCellImageDrop.ts',
  'features/cellImages/CellImageOverlay.tsx',
  'features/cellImages/cellImageTokens.ts',
  'features/cellImages/cellImageKey.ts',
  'types/cellImage.ts',
];

// Match calls/assignments rather than prose. Shared sink identifiers come from SINK_TEXT so the scan
// does not match its own instrumentation comments.
const PROHIBITED_SOURCE_PATTERNS: Array<{ label: string; pattern: RegExp }> = [
  { label: SINK_TEXT.localStore, pattern: new RegExp(`\\b${SINK_TEXT.localStore}\\b`) },
  { label: SINK_TEXT.sessionStore, pattern: new RegExp(`\\b${SINK_TEXT.sessionStore}\\b`) },
  {
    label: SINK_TEXT.indexedDatabase,
    pattern: new RegExp(`\\b${SINK_TEXT.indexedDatabase}\\b`, 'i'),
  },
  { label: 'document.cookie', pattern: /document\s*\.\s*cookie/ },
  {
    label: `${SINK_TEXT.networkFetch} call`,
    pattern: new RegExp(`\\b${SINK_TEXT.networkFetch}\\s*\\(`),
  },
  { label: 'XMLHttpRequest', pattern: /\bXMLHttpRequest\b/ },
  { label: 'sendBeacon', pattern: /\bsendBeacon\b/ },
  { label: SINK_TEXT.httpClientPackage, pattern: new RegExp(`\\b${SINK_TEXT.httpClientPackage}\\b`) },
  {
    label: SINK_TEXT.backendSdkPackage,
    pattern: new RegExp(`\\b${SINK_TEXT.backendSdkPackage}\\b`, 'i'),
  },
  { label: 'WebSocket', pattern: /\bWebSocket\b/ },
  { label: 'serialization of feature state', pattern: /JSON\s*\.\s*(stringify|parse)\s*\(/ },
  { label: 'byte read', pattern: /\bFileReader\b|readAs[A-Z]|\.\s*arrayBuffer\s*\(|\.\s*text\s*\(\)/ },
  { label: 'asynchronous admission', pattern: /\basync\b|\bawait\s+\w|\bPromise\b/ },
  { label: 'root-alias import prefix', pattern: new RegExp(`from '${ROOT_ALIAS}`) },
  { label: 'Redux binding', pattern: /useAppSelector|useAppDispatch|useSelector|useDispatch/ },
  { label: 'unsafe markup sink', pattern: /dangerouslySetInnerHTML|<iframe|<object/ },
  // Production modules must not set tabIndex explicitly; the native dismiss button's default
  // focusability is verified separately in rendered overlay tests.
  { label: 'tab-order attribute', pattern: /\btabIndex\b/ },
];

// Cell/Grid/App cannot be imported while existing root-alias imports are unresolved. Read them as text
// to pin wiring without adding a Jest mapper that would hide the production resolution failure.
declare const __dirname: string;
declare function require(moduleId: string): unknown;

interface SourceFileSystem {
  readFileSync(path: string, encoding: 'utf8'): string;
  readdirSync(path: string): string[];
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
    // A key-scoped snapshot prevents an unrelated cell from re-rendering; an unscoped map value would
    // broadcast React state to every cell.
    expect(cellSourceCollapsed).toContain(
      'const { image: cellImage, clearCellImage } = useCellImages(imageKey);',
    );
    expect(cellSourceCollapsed).toContain(
      'const { dragHandlers, isDragActive, isRejecting } = useCellImageDrop(imageKey);',
    );
    expect(cellSourceCollapsed).toContain('clearCellImage(imageKey);');
    expect(cellSourceCollapsed).not.toContain('useCellImages();');
    expect(cellSource).not.toContain('getCellImage');
  });

  it('spreads the drag handlers onto the element that is the cell', () => {
    // Anchored to className="cell" rather than to "a div somewhere in the file", so moving the spread
    // onto a nested wrapper — which would change which box the drop and the affordance belong to —
    // fails here.
    expect(cellSource).toMatch(/<div\s+className="cell"[^>]*\{\.\.\.dragHandlers\}[^>]*>/);
  });

  it('lets a drag in progress outrank a standing refusal and reads every value from the token module', () => {
    // Both flags are consulted, and the order between them is the assertion: a refused drop keeps this
    // cell's refusal standing for the notice's whole lifetime, so an acceptable payload dragged back
    // over that same cell arrives with both flags true. The hook advertises 'copy' for it, so the
    // outline has to agree with the cursor rather than repeat a complaint the notice is still making.
    expect(cellSourceCollapsed).toContain(
      'const affordance = isDragActive ? dragActiveOutline : isRejecting ? dragRejectOutline : undefined;',
    );
    // The superseded order painted a refusal over a payload the cell would in fact accept.
    expect(cellSource).not.toContain('isRejecting ? dragRejectOutline : isDragActive');
    expect(cellSourceCollapsed).toContain('outlineWidth: CELL_IMAGE_TOKENS.dropOutlineWidth');
    expect(cellSourceCollapsed).toContain('outlineStyle: CELL_IMAGE_TOKENS.dropOutlineStyle');
    expect(cellSourceCollapsed).toContain('outlineColor: CELL_IMAGE_TOKENS.dropActiveOutlineColor');
    expect(cellSourceCollapsed).toContain('backgroundColor: CELL_IMAGE_TOKENS.dropActiveBackground');
    expect(cellSourceCollapsed).toContain('outlineColor: CELL_IMAGE_TOKENS.dropRejectOutlineColor');
    // No colour, width, inset, z-index or duration literal may appear at a usage site.
    expect(cellSource).not.toMatch(/#[0-9A-Fa-f]{3,8}|rgba?\(|\d+px|\d+ms/);
  });

  it('animates both affordances over the motion token and leaves the idle path untransitioned', () => {
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
    // Pin idle style identity and require the non-idle branch to merge the caller style with the
    // computed affordance, with the shorthand withdrawal applied to the caller's half of that merge.
    expect(cellSourceCollapsed).toContain(
      'visibleImage === undefined && affordance === undefined ? style : { ...withoutSupersededShorthands(style, affordance), ...(visibleImage !== undefined ? cellImageContainingBlock : undefined), ...affordance, };',
    );
    // Spreading the caller's object unfiltered is what left a cell permanently unpainted once a drag
    // had been over it, so the superseded form must not come back.
    expect(cellSourceCollapsed).not.toContain(
      'affordance === undefined ? style : { ...style, ...(visibleImage',
    );
  });

  it('withdraws only the caller shorthands the applied affordance would collide with', () => {
    // Every longhand either affordance object contributes is mapped to its shorthand family, so the
    // family shorthand can step aside for exactly as long as the affordance is applied. Leaving both in
    // one object makes React's next update destructive rather than additive.
    expect(cellSourceCollapsed).toContain(
      'const affordanceShorthandFamilies: Readonly<Record<string, string | undefined>> = {',
    );
    expect(cellSourceCollapsed).toContain("backgroundColor: 'background',");
    expect(cellSourceCollapsed).toContain("outlineWidth: 'outline',");
    expect(cellSourceCollapsed).toContain("outlineStyle: 'outline',");
    expect(cellSourceCollapsed).toContain("outlineColor: 'outline',");
    expect(cellSourceCollapsed).toContain("transitionProperty: 'transition',");
    expect(cellSourceCollapsed).toContain("transitionDuration: 'transition',");
    // Every longhand the two affordance objects actually write has an entry, or the family it belongs
    // to would still be left standing beside it.
    const affordanceLonghands = [
      'transitionProperty',
      'transitionDuration',
      'outlineWidth',
      'outlineStyle',
      'outlineColor',
      'backgroundColor',
    ];
    affordanceLonghands.forEach((longhand) => {
      expect(cellSourceCollapsed).toMatch(new RegExp(`${longhand}: '(background|outline|transition)',`));
    });
    // Driven off the applied affordance's own keys, so the refusal affordance — which writes no
    // background longhand — leaves the caller's background exactly where it was.
    expect(cellSourceCollapsed).toContain('Object.keys(applied).forEach(');
    // The caller's object is copied, never mutated: the idle branch hands that same object back.
    expect(cellSourceCollapsed).toContain('const resolved: Record<string, unknown> = { ...base };');
    expect(cellSourceCollapsed).toContain('if (applied === undefined) { return base; }');
    expect(cellSource).not.toMatch(/delete\s+base\[/);
    expect(cellSource).not.toMatch(/delete\s+style\[/);
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

  it('leaves the pre-existing editing behaviour untouched and adds no focusable node of its own', () => {
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
    // Cell.tsx adds no tabIndex to its root; native focusability of the overlay's button is verified
    // separately in rendered tests.
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
      `${ROOT_ALIAS}store`,
      `${ROOT_ALIAS}store/workbookSlice`,
      `${ROOT_ALIAS}utils/cellFormatting`,
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
    // Grid's React key supplies the row/column basis; cellImageKey adds the worksheet id without
    // reading the divergent cells collection.
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

    // No timer, microtask, or waitFor: the object URL and rendered image are observable before
    // fireEvent.drop returns.
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
    // Zero, not one: the type gate runs before the mint site, so a refused payload leaves no object
    // URL minted and no blob-backed image resource retained. That is the exact claim this assertion
    // supports — the dropped File was already resident before the drop handler ran, and the refusal
    // does record a rejection for the notice, so "costs nothing" would be the wrong reading.
    expect(createObjectUrlSpy).not.toHaveBeenCalled();
    expect(rejectionOf('harness-cell')).toEqual({ reason: UNSUPPORTED_TYPE, rejecting: 'true' });
    expect(screen.getByRole('status')).toHaveTextContent('notes.txt');
  });

  it('refuses a file above the byte ceiling before any object URL is minted', () => {
    renderHarness(cellImageKey('ws-1', 2, 1));

    dropFiles(screen.getByTestId('harness-cell'), [oversizeRasterFile('huge.png')]);

    expect(screen.queryAllByRole('img')).toHaveLength(0);
    expect(createObjectUrlSpy).not.toHaveBeenCalled();
    expect(rejectionOf('harness-cell')).toEqual({ reason: TOO_LARGE, rejecting: 'true' });

    // The notice names the file and states the rule the gate actually applied. The bound is quoted
    // inclusively, because a file of exactly the ceiling is accepted — the case below asserts that
    // from the other side, and this assertion is what keeps the wording from contradicting it. The
    // figure is derived here rather than copied, so a changed ceiling cannot leave stale copy behind.
    const notice = screen.getByRole('status');
    expect(notice).toHaveTextContent('huge.png');
    expect(notice).toHaveTextContent(`at most ${MAX_IMAGE_BYTES / (1024 * 1024)} MiB`);
  });

  it('accepts a file exactly at the byte ceiling, so the limit is inclusive', () => {
    renderHarness(cellImageKey('ws-1', 2, 2));

    dropFiles(screen.getByTestId('harness-cell'), [
      sizedRasterFile('at-the-limit.png', MAX_IMAGE_BYTES),
    ]);

    expect(screen.getByAltText('at-the-limit.png')).toBeInTheDocument();
    expect(createObjectUrlSpy).toHaveBeenCalledTimes(1);
  });

  it('refuses an empty file that claims an accepted type, and says why', () => {
    renderHarness(cellImageKey('ws-1', 2, 3));

    dropFiles(screen.getByTestId('harness-cell'), [emptyRasterFile('empty.png')]);

    // Admitting it would leave a broken picture in the cell and an object URL pinning a blob that
    // can never be shown, so the refusal is made on the one content fact a metadata gate has: a
    // file of no length cannot be an image, whatever it declares itself to be.
    expect(screen.queryAllByRole('img')).toHaveLength(0);
    expect(createObjectUrlSpy).not.toHaveBeenCalled();
    // The reason stays inside the feature's two-value vocabulary while the notice is specific
    // about what actually happened, so a user is never told an accepted format is unsupported.
    expect(rejectionOf('harness-cell')).toEqual({ reason: UNSUPPORTED_TYPE, rejecting: 'true' });
    const notice = screen.getByRole('status');
    expect(notice).toHaveTextContent('empty.png');
    expect(notice).toHaveTextContent('empty');
    // Anchored on the unit rather than on a phrase, so this stays a real check on which rule was
    // quoted even if the size wording is reworded: an empty file is not a file that is too large.
    expect(notice.textContent).not.toContain('MiB');
  });

  it('dismissing a picture removes it and releases exactly the URL that was minted', () => {
    renderHarness(cellImageKey('ws-1', 3, 0));
    dropFiles(screen.getByTestId('harness-cell'), [rasterFile('photo.png')]);
    expect(createObjectUrlSpy).toHaveBeenCalledTimes(1);

    fireEvent.click(dismissControlFor('photo.png'));

    expect(screen.queryAllByRole('img')).toHaveLength(0);
    expect(revokeObjectUrlSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectUrlSpy).toHaveBeenCalledWith(mintedUrl(1));
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

    // Blur must restore the same URL without minting or releasing another one.
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

  it('arms a cell whose refusal is still standing, so both flags report at once', () => {
    renderHarness(cellImageKey('ws-5', 0, 5));
    const cell = screen.getByTestId('harness-cell');

    // A refused drop leaves this cell's refusal standing for the notice's whole token lifetime.
    dropFiles(cell, [textFile('notes.txt')]);
    expect(dragFlagOf('harness-cell', 'rejecting')).toBe('true');
    expect(dragFlagOf('harness-cell', 'drag-active')).toBe('false');

    // Dragging an acceptable picture back over that same cell arms it again while the complaint still
    // stands, which is the state Cell.tsx's affordance precedence exists to resolve — reachable rather
    // than hypothetical, and the reason the outline may not simply follow the refusal flag.
    const transfer = dataTransferFor([rasterFile('photo.png')]);
    const enterEvent = createEvent.dragEnter(cell, { dataTransfer: transfer });
    fireEvent(cell, enterEvent);

    // The cursor already promises acceptance here, so an outline that followed the refusal instead
    // would contradict it.
    expect(transfer.dropEffect).toBe('copy');
    expect(dragFlagOf('harness-cell', 'drag-active')).toBe('true');
    // The refusal is not cancelled by the hover: it retires on its own timer or on a good drop.
    expect(dragFlagOf('harness-cell', 'rejecting')).toBe('true');
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

  it('clears a drifted affordance as soon as the gesture verifiably leaves the cell', () => {
    render(
      <CellImageProvider>
        <HarnessCell imageKey={cellImageKey('ws-5', 0, 6)} />
        <div data-testid="outside-the-cell" />
      </CellImageProvider>,
    );
    const cell = screen.getByTestId('harness-cell');
    const transfer = () => ({ dataTransfer: dataTransferFor([rasterFile('photo.png')]) });

    // Two enters model the cell and a node inside it. A child's matching leave can go missing — a
    // node unmounted mid-drag never reports one — which is how the count drifts above what the
    // pointer is actually inside.
    fireEvent.dragEnter(cell, transfer());
    fireEvent.dragEnter(cell, transfer());
    expect(dragFlagOf('harness-cell', 'drag-active')).toBe('true');

    // This leave names a destination outside the cell, so the gesture's whereabouts are known rather
    // than inferred and every counted enter is spent at once. Decrementing alone would leave the
    // affordance lit here with the pointer already elsewhere, for as many further leaves as the count
    // had drifted by.
    dragLeaveTowards(cell, screen.getByTestId('outside-the-cell'), [rasterFile('photo.png')]);
    expect(dragFlagOf('harness-cell', 'drag-active')).toBe('false');

    // Cleared, not stuck off: the next real drag arms the cell again on its first enter.
    fireEvent.dragEnter(cell, transfer());
    expect(dragFlagOf('harness-cell', 'drag-active')).toBe('true');
  });

  it('treats a move to a node inside the same cell as a crossing, never as an exit', () => {
    renderHarness(cellImageKey('ws-5', 0, 7));
    const cell = screen.getByTestId('harness-cell');
    const transfer = () => ({ dataTransfer: dataTransferFor([rasterFile('photo.png')]) });

    // The real sequence for moving onto a child: the child's enter bubbles first, then the cell
    // reports a leave whose destination is that child.
    fireEvent.dragEnter(cell, transfer());
    fireEvent.dragEnter(cell, transfer());

    dragLeaveTowards(cell, within(cell).getByTestId('cell-value'), [rasterFile('photo.png')]);

    // Clearing on any leave at all would flicker the outline off on every child crossing, which is
    // the whole reason enters are counted; a destination inside the cell has to stay a crossing.
    expect(dragFlagOf('harness-cell', 'drag-active')).toBe('true');
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

  // Candidate selection applies every metadata gate, so an oversized or empty leading raster cannot
  // hide a later acceptable file.
  it('passes over an oversized image for a smaller acceptable one later in the same drop', () => {
    renderHarness(cellImageKey('ws-5', 1, 0));

    dropFiles(screen.getByTestId('harness-cell'), [
      oversizeRasterFile('huge.png'),
      rasterFile('wanted.png'),
    ]);

    expect(screen.getAllByRole('img')).toHaveLength(1);
    expect(screen.getByAltText('wanted.png')).toBeInTheDocument();
    expect(createObjectUrlSpy).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('status')).toBeNull();
    expect(rejectionOf('harness-cell')).toEqual({ reason: '', rejecting: 'false' });
  });

  it('passes over an empty image for an acceptable one later in the same drop', () => {
    renderHarness(cellImageKey('ws-5', 1, 1));

    dropFiles(screen.getByTestId('harness-cell'), [
      emptyRasterFile('empty.png'),
      rasterFile('wanted.png'),
    ]);

    expect(screen.getByAltText('wanted.png')).toBeInTheDocument();
    expect(createObjectUrlSpy).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('keeps the earlier image when it is acceptable and a later one is not', () => {
    renderHarness(cellImageKey('ws-5', 1, 2));

    dropFiles(screen.getByTestId('harness-cell'), [
      rasterFile('wanted.png'),
      oversizeRasterFile('huge.png'),
    ]);

    expect(screen.getByAltText('wanted.png')).toBeInTheDocument();
    expect(createObjectUrlSpy).toHaveBeenCalledTimes(1);
  });

  it('refuses a drop whose every raster candidate is oversized, naming the first of them', () => {
    renderHarness(cellImageKey('ws-5', 1, 3));

    dropFiles(screen.getByTestId('harness-cell'), [
      textFile('notes.txt'),
      oversizeRasterFile('huge.png'),
      oversizeRasterFile('also-huge.png'),
    ]);

    expect(screen.queryAllByRole('img')).toHaveLength(0);
    expect(createObjectUrlSpy).not.toHaveBeenCalled();
    // The size reason, not the type one: these files are the right type and the wrong length, and
    // the notice names the first raster candidate rather than the text file in front of it.
    expect(rejectionOf('harness-cell')).toEqual({ reason: TOO_LARGE, rejecting: 'true' });
    expect(screen.getByRole('status')).toHaveTextContent('huge.png');
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
    // away either, but nothing is accepted, minted, or announced.
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

  it('names a refused payload that carried no file name with the same neutral label', () => {
    renderHarness(cellImageKey('ws-6', 0, 2));

    dropFiles(screen.getByTestId('harness-cell'), [textFile('')]);

    // "Skipped :" would announce nothing, so the notice falls back to the same wording an unnamed
    // picture's alternative text uses — one fallback rule across both surfaces.
    expect(screen.getByRole('status')).toHaveTextContent(`Skipped ${NEUTRAL_FILE_LABEL}`);
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

  it('floats the notice in a corner instead of stretching it across the viewport', () => {
    renderHarness(cellImageKey('ws-6', 3, 0));

    dropFiles(screen.getByTestId('harness-cell'), [textFile('notes.txt')]);

    const notice = screen.getByRole('status');
    const declared = (property: string): string => notice.style.getPropertyValue(property);

    // Released from the start edge, so the box takes only the width its message needs. Pinned to both
    // inline edges it was as wide as the window, which is what put it across a grid row; released and
    // bounded, it cannot reach further than its own maximum however long the message is.
    expect(declared('inset-inline-start')).toBe('auto');
    expect(declared('inset-inline-end')).toBe(CELL_IMAGE_TOKENS.statusStripInset);

    // Two bounds, not one. The fixed bound keeps the notice compact where there is room; the relative
    // bound keeps it inside a narrow viewport, where a box wider than the screen would be pushed off
    // the start edge and would carry its own text off with it. Measured against the border box, so the
    // padding counts toward the bound instead of being added outside it.
    expect(declared('box-sizing')).toBe('border-box');
    expect(declared('max-inline-size')).toBe(
      `min(${CELL_IMAGE_TOKENS.statusStripMaxInlineSize}, calc(100% - ${CELL_IMAGE_TOKENS.statusStripInset} * 2))`,
    );
    // A percentage of the containing block, never a viewport unit: vh/vw ignore the scrollbar and
    // would let the notice overflow the axis it is trying to stay inside.
    expect(declared('max-inline-size')).not.toMatch(/\d(vw|vh|vmin|vmax)\b/);

    // Inset from the bottom rather than flush against it, and still measured from the block END so a
    // notice never grows upward into the grid as its text wraps.
    expect(declared('inset-block-end')).toBe(CELL_IMAGE_TOKENS.statusStripInset);
    expect(declared('inset-block-start')).toBe('auto');

    // Interior space, so the text no longer starts on the very edge of the box.
    expect(declared('padding-block')).toBe(CELL_IMAGE_TOKENS.statusStripPaddingBlock);
    expect(declared('padding-inline')).toBe(CELL_IMAGE_TOKENS.statusStripPaddingInline);
    expect(declared('border-radius')).toBe(CELL_IMAGE_TOKENS.statusStripBorderRadius);
    expect(shadowOf(notice)).toBe(
      asDeclared('box-shadow', CELL_IMAGE_TOKENS.statusStripShadow),
    );

    // Every one of those is a logical property, so the corner it occupies follows the writing mode
    // rather than being hard-coded to the left or the right of the screen.
    expect(notice.getAttribute('style')).not.toMatch(/(^|;)\s*(left|right|top|bottom)\s*:/);

    // None of the layout work weakened what the notice is FOR: it still announces politely, still
    // floats over the grid without displacing it, and still cannot be hit.
    expect(notice).toHaveAttribute('aria-live', 'polite');
    expect(notice).toHaveStyle({
      position: 'fixed',
      pointerEvents: 'none',
      zIndex: CELL_IMAGE_TOKENS.overlayZIndex,
    });
    expect(notice.style.getPropertyValue('background-color')).toBe(
      asDeclared('background-color', CELL_IMAGE_TOKENS.statusStripBackground),
    );
    expect(notice.style.getPropertyValue('color')).toBe(
      asDeclared('color', CELL_IMAGE_TOKENS.statusStripColor),
    );
    // A long file name still has to break rather than force the box wider than its maximum.
    expect(declared('overflow-wrap')).toBe('break-word');
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
    // The non-interactive layer passes clicks through; only the dismiss button opts back into
    // pointer events.
    expect(layer).toHaveStyle({ pointerEvents: 'none' });
    expect(layer).toHaveStyle({ zIndex: String(CELL_IMAGE_TOKENS.overlayZIndex) });
  });

  it('scales the picture inside the cell box instead of resizing the cell', () => {
    renderHarness(cellImageKey('ws-10', 0, 0));
    dropFiles(screen.getByTestId('harness-cell'), [rasterFile('photo.png')]);

    // max-* and object-fit contain scale the image within the absolutely positioned, clipped layer
    // without affecting grid geometry.
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
    expect(dismiss).toHaveStyle({
      fontSize: CELL_IMAGE_TOKENS.dismissButtonSize,
      lineHeight: CELL_IMAGE_TOKENS.dismissButtonSize,
    });
  });

  it('names a picture whose payload carried no file name instead of exposing a blank', () => {
    renderHarness(cellImageKey('ws-10', 0, 4));
    dropFiles(screen.getByTestId('harness-cell'), [rasterFile('')]);

    // A drag can legitimately deliver an empty name, which would otherwise reach a screen reader as
    // an empty alternative text. The picture is reached by role because the name under test is
    // exactly what a blank would erase.
    const picture = screen.getByRole('img');
    expect(picture.getAttribute('alt')).not.toBe('');
    expect(picture).toHaveAttribute('alt', NEUTRAL_FILE_LABEL);

    // The control is announced with the same fallback, and it drops the now-redundant noun so the
    // neutral label still reads as a phrase.
    const dismiss = screen.getByRole('button', { name: `Remove ${NEUTRAL_FILE_LABEL}` });
    expect(dismiss).toHaveAttribute('type', 'button');
    // Not "Remove image" with the name simply missing: the fallback replaces the name rather than
    // leaving a dangling noun behind, and this is the form an unguarded label would produce.
    expect(screen.queryByRole('button', { name: 'Remove image' })).toBeNull();

    // Nothing else about an unnamed picture differs: removing it releases exactly the URL it held.
    fireEvent.click(dismiss);
    expect(screen.queryAllByRole('img')).toHaveLength(0);
    expect(revokedUrls()).toEqual([mintedUrl(1)]);
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

    // Assert the callback count directly because a duplicate clear would leave the same DOM result.
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

  it('renders no tab-order attribute of its own and keeps its control natively reachable', () => {
    // The feature sets no tabIndex attribute, but native dismiss buttons remain sequentially
    // focusable. This rendered grid distinguishes explicit tab-order changes from platform button
    // defaults.
    render(
      <CellImageProvider>
        <div role="grid" tabIndex={0}>
          <div role="row">
            <HarnessCell imageKey={cellImageKey('ws-11', 0, 0)} testId="cell-a" />
            <HarnessCell imageKey={cellImageKey('ws-11', 0, 1)} testId="cell-b" />
          </div>
        </div>
      </CellImageProvider>,
    );

    dropFiles(screen.getByTestId('cell-a'), [rasterFile('first.png')]);
    dropFiles(screen.getByTestId('cell-b'), [rasterFile('second.png')]);
    expect(screen.getAllByRole('img')).toHaveLength(2);

    const grid = screen.getByRole('grid');
    // The grid's own declared tab stop remains unchanged.
    expect(grid.tabIndex).toBe(0);
    // Verify that no descendant receives an explicit tabindex attribute; native button tabIndex is
    // checked separately below.
    const renderedNodes = [
      ...within(grid).getAllByRole('row'),
      ...within(grid).getAllByRole('generic'),
      ...within(grid).getAllByRole('img'),
      ...within(grid).getAllByRole('button'),
    ];
    expect(renderedNodes.filter((node) => node.hasAttribute('tabindex'))).toEqual([]);

    const controls = screen.getAllByRole('button');
    expect(controls).toHaveLength(2);
    // Real buttons, so their reachability comes from the platform rather than from an attribute
    // this feature wrote. tabIndex reads 0 because that is a button's own default — which is
    // exactly what the attribute assertion above distinguishes from a value set here.
    expect(controls.map((control) => control.tagName)).toEqual(['BUTTON', 'BUTTON']);
    expect(controls.map((control) => control.getAttribute('type'))).toEqual(['button', 'button']);
    expect(controls.map((control) => control.hasAttribute('tabindex'))).toEqual([false, false]);
    expect(controls.map((control) => control.tabIndex)).toEqual([0, 0]);
    // Nothing else the feature renders is focusable. Between them these three queries and the
    // ones above cover every node in the tree: the grid, its row, the pictures, the controls,
    // and every remaining div and span, including each overlay's own layer.
    expect(within(grid).getAllByRole('row').map((row) => row.tabIndex)).toEqual([-1]);
    expect(screen.getAllByRole('img').map((picture) => picture.tabIndex)).toEqual([-1, -1]);
    expect(within(grid).getAllByRole('generic').every((node) => node.tabIndex === -1)).toBe(true);

    // Reachable is not the same as decorative. Once the control holds focus it shows its focus
    // ring, and activating it removes that one picture, releasing exactly the URL it held.
    const dismiss = dismissControlFor('first.png');
    act(() => {
      dismiss.focus();
    });
    expect(dismiss).toHaveFocus();
    expect(shadowOf(dismiss)).toBe(asDeclared('box-shadow', FOCUS_RING));

    fireEvent.click(dismiss);

    expect(screen.getAllByRole('img')).toHaveLength(1);
    expect(revokedUrls()).toEqual([mintedUrls()[0]]);
    const surviving = dismissControlFor('second.png');
    expect(surviving.hasAttribute('tabindex')).toBe(false);
    expect(surviving.tabIndex).toBe(0);
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

describe('cell image overlay focus handover and undecodable pictures', () => {
  // Modelled on Grid.tsx, whose grid element is the application's single declared tab stop. Removing
  // the focused control has to leave a keyboard user inside that stop rather than on the document
  // body, which is where a browser puts focus when the element holding it disappears.
  const renderInGrid = (imageKey: string) =>
    render(
      <CellImageProvider>
        <div role="grid" tabIndex={0}>
          <div role="row">
            <HarnessCell imageKey={imageKey} />
          </div>
        </div>
      </CellImageProvider>,
    );

  it('hands focus back to the grid when the control holding it is removed', () => {
    renderInGrid(cellImageKey('ws-12', 0, 0));
    dropFiles(screen.getByTestId('harness-cell'), [rasterFile('photo.png')]);

    const dismiss = dismissControlFor('photo.png');
    act(() => {
      dismiss.focus();
    });
    expect(dismiss).toHaveFocus();

    // The click a browser synthesises from Enter or Space on a button, which is the path a keyboard
    // dismissal actually takes.
    fireEvent.click(dismiss);

    const grid = screen.getByRole('grid');
    expect(grid).toHaveFocus();
    expect(document.body).not.toHaveFocus();
    // Nothing new was made focusable to achieve it: the stop the grid already declared is reused.
    expect(grid.tabIndex).toBe(0);
    expect(within(grid).getAllByRole('row').some((row) => row.hasAttribute('tabindex'))).toBe(false);
    expect(within(grid).getAllByRole('generic').some((node) => node.hasAttribute('tabindex'))).toBe(
      false,
    );
  });

  it('hands focus over on a pointer dismissal as well, which is the same activation path', () => {
    renderInGrid(cellImageKey('ws-12', 0, 1));
    dropFiles(screen.getByTestId('harness-cell'), [rasterFile('photo.png')]);

    fireEvent.click(dismissControlFor('photo.png'));

    expect(screen.getByRole('grid')).toHaveFocus();
    expect(screen.queryAllByRole('img')).toHaveLength(0);
    // The value the picture was covering is back, untouched by any of it.
    expect(screen.getByTestId('cell-value')).toHaveTextContent('42');
  });

  it('leaves focus exactly where it was when no ancestor is a tab stop', () => {
    // Rendered outside any grid, which is how a consumer that declares no tab stop would mount it.
    // Reaching for the nearest one must degrade to doing nothing rather than to guessing.
    const onDismiss = jest.fn<void, []>();
    render(
      <div className="cell">
        <CellImageOverlay entry={entryFor('photo.png', 'blob:isolated')} onDismiss={onDismiss} />
      </div>,
    );

    const dismiss = dismissControlFor('photo.png');
    act(() => {
      dismiss.focus();
    });
    fireEvent.click(dismiss);

    expect(onDismiss).toHaveBeenCalledTimes(1);
    // onDismiss is a spy here, so the control survives its own activation and should still hold focus.
    expect(dismiss).toHaveFocus();
  });

  it('restores focus through an existing tab stop, before the control can unmount', () => {
    const overlaySource = readClientSource('features/cellImages/CellImageOverlay.tsx');

    // Written against the rendered attribute in lower case, and excluding negative values so a node
    // deliberately taken out of the tab order is never chosen.
    expect(overlaySource).toContain(
      'const FOCUSABLE_ANCESTOR_SELECTOR = \'[tabindex]:not([tabindex^="-"])\';',
    );
    expect(overlaySource).toContain('control.closest(FOCUSABLE_ANCESTOR_SELECTOR)');
    expect(overlaySource).toContain('ancestor.focus({ preventScroll: true });');

    // Ordering is the whole point: after the callback there is no control left to walk up from.
    const restoreSite = overlaySource.indexOf('restoreFocusToAncestor(dismissControlRef.current);');
    const dismissSite = overlaySource.indexOf('onDismiss();');
    expect(restoreSite).toBeGreaterThan(-1);
    expect(dismissSite).toBeGreaterThan(restoreSite);
  });

  it('replaces a picture the browser could not decode with a compact named indicator', () => {
    renderInGrid(cellImageKey('ws-12', 0, 2));
    dropFiles(screen.getByTestId('harness-cell'), [rasterFile('corrupt.png')]);

    const picture = screen.getByRole('img', { name: 'corrupt.png' });
    // The one signal a metadata gate never had: the browser reporting, of its own accord, that it
    // finished trying. Nothing is read from the file and no decode is attempted by this feature.
    fireEvent.error(picture);

    // Out of flow, so neither the broken-picture glyph nor the file name is drawn over the value, and
    // the element leaves the accessibility tree with them.
    expect(picture).toHaveStyle({ display: 'none' });
    expect(screen.queryByRole('img', { name: 'corrupt.png' })).toBeNull();

    const indicator = screen.getByRole('img', { name: 'corrupt.png could not be displayed' });
    expect(screen.getAllByRole('img')).toEqual([indicator]);
    expect(indicator).toHaveStyle({
      inlineSize: CELL_IMAGE_TOKENS.undisplayableIndicatorSize,
      blockSize: CELL_IMAGE_TOKENS.undisplayableIndicatorSize,
      fontSize: CELL_IMAGE_TOKENS.undisplayableGlyphSize,
      lineHeight: CELL_IMAGE_TOKENS.undisplayableGlyphSize,
    });

    // Held to the removal control's own footprint. This is the guard on a defect measured in a real
    // browser rather than in jsdom, which has no layout to measure: at 24px the disc filled 24 of a
    // default cell's 27 usable pixels and covered the whole of the value's text box, so a marker
    // deliberately kept out of the centre ended up doing exactly what centring it would have done.
    // Pinning the two to one step is what stops it being re-inflated past the box it has to share.
    expect(CELL_IMAGE_TOKENS.undisplayableIndicatorSize).toBe(CELL_IMAGE_TOKENS.dismissButtonSize);

    // Filled rather than ringed, matching that control: at this size a 2px ring would leave a 10px
    // interior and clip the glyph it exists to frame.
    expect(indicator.style.getPropertyValue('border-style')).toBe('');
    expect(indicator.style.getPropertyValue('border-width')).toBe('');
    expect(indicator.style.getPropertyValue('background-color')).toBe(
      asDeclared('background-color', CELL_IMAGE_TOKENS.dropRejectOutlineColor),
    );
    expect(indicator.style.getPropertyValue('color')).toBe(
      asDeclared('color', CELL_IMAGE_TOKENS.statusStripColor),
    );

    // Taken out of the layer's centring and pinned to a corner instead. Centred, an opaque disc lands
    // exactly where the layer centres what it holds and hides the value behind the very marker that
    // exists to say the picture is not being drawn; the corner leaves the majority of it visible.
    expect(indicator).toHaveStyle({ position: 'absolute' });
    expect(indicator.style.getPropertyValue('inset-block-end')).toBe(
      CELL_IMAGE_TOKENS.dismissButtonInset,
    );
    expect(indicator.style.getPropertyValue('inset-inline-start')).toBe(
      CELL_IMAGE_TOKENS.dismissButtonInset,
    );
    // The opposite corner from the removal control, so the two can never sit on top of each other.
    const control = dismissControlFor('corrupt.png');
    expect(control.style.getPropertyValue('inset-block-start')).toBe(
      CELL_IMAGE_TOKENS.dismissButtonInset,
    );
    expect(control.style.getPropertyValue('inset-inline-end')).toBe(
      CELL_IMAGE_TOKENS.dismissButtonInset,
    );

    // The cell's own value is still there beside the marker, and nothing was written to the cell.
    expect(screen.getByTestId('cell-value')).toHaveTextContent('42');

    // Still removable, so the reading is actionable rather than a dead end, and the URL it held is
    // released exactly once.
    fireEvent.click(dismissControlFor('corrupt.png'));
    expect(screen.queryAllByRole('img')).toHaveLength(0);
    expect(revokedUrls()).toEqual([mintedUrl(1)]);
  });

  it('shows the next picture normally after a decode failure, with no reset to perform', () => {
    renderInGrid(cellImageKey('ws-12', 0, 3));
    const cell = screen.getByTestId('harness-cell');
    dropFiles(cell, [rasterFile('corrupt.png')]);
    fireEvent.error(screen.getByRole('img', { name: 'corrupt.png' }));
    expect(screen.getByRole('img', { name: 'corrupt.png could not be displayed' })).toBeInTheDocument();

    dropFiles(cell, [rasterFile('replacement.png')]);

    // The failure was recorded against the URL that failed, so a different URL simply stops matching:
    // the replacement starts clean without any explicit reset, and the superseded URL is released.
    const replacement = screen.getByRole('img', { name: 'replacement.png' });
    expect(replacement).toHaveStyle({ display: 'block' });
    expect(screen.getAllByRole('img')).toEqual([replacement]);
    expect(revokedUrls()).toEqual([mintedUrl(1)]);
  });
});


describe('cell image design tokens', () => {
  // The feature has no stylesheet, so its mutable design values are centralized in the frozen token
  // object.
  it('is frozen, so no component can restyle the feature by writing to it', () => {
    expect(Object.isFrozen(CELL_IMAGE_TOKENS)).toBe(true);
  });

  it('drives every affordance transition from the one motion token', () => {
    renderHarness(cellImageKey('ws-11', 0, 0));
    dropFiles(screen.getByTestId('harness-cell'), [rasterFile('photo.png')]);

    // The removal control's own state treatment animates over the token duration. The cell's
    // drag affordance reads the same token; that one lives in Cell.tsx, which this suite cannot
    // import because of the repository's unresolved root-alias specifiers, so it is verified by
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

describe('cell image real raster payloads', () => {
  // The accepted path, driven with actual image bytes rather than a label on some text. The gates do
  // not read a file, so this cannot change their verdict — what it proves is that a genuine raster
  // survives the pipeline intact: the very File that was dropped is what the blob URL is minted from,
  // and the length the entry records is the file's true byte length rather than a fixture's guess.
  it('renders a genuine one-pixel PNG from the exact File that was dropped', () => {
    renderHarness(cellImageKey('ws-12', 0, 0));
    const file = realRasterFile('one-pixel.png', MINIMAL_PNG_BYTES, 'image/png');
    expect(file.size).toBe(MINIMAL_PNG_BYTES.length);

    dropFiles(screen.getByTestId('harness-cell'), [file]);

    const image = screen.getByRole('img');
    expect(image).toHaveAttribute('alt', 'one-pixel.png');
    expect(image).toHaveAttribute('src', mintedUrl(1));
    // Passing the original File proves there is no application-side copy, slice, or re-encoding
    // before URL creation.
    expect(createObjectUrlSpy).toHaveBeenCalledTimes(1);
    expect(createObjectUrlSpy).toHaveBeenCalledWith(file);
  });

  it('renders a genuine one-pixel GIF, so more than one real container is exercised', () => {
    renderHarness(cellImageKey('ws-12', 0, 1));
    const file = realRasterFile('one-pixel.gif', MINIMAL_GIF_BYTES, 'image/gif');

    dropFiles(screen.getByTestId('harness-cell'), [file]);

    expect(screen.getByAltText('one-pixel.gif')).toHaveAttribute('src', mintedUrl(1));
    expect(createObjectUrlSpy).toHaveBeenCalledWith(file);
  });

  it('refuses a real PNG whose declared type is not on the allow-list', () => {
    renderHarness(cellImageKey('ws-12', 0, 2));
    // Real image bytes, mislabelled. The allow-list is a policy about what the application will
    // display, not a claim about what the bytes are, so the declared type decides — and this is the
    // honest boundary of a metadata-only gate: it can be lied to in both directions.
    const file = realRasterFile('mislabelled.png', MINIMAL_PNG_BYTES, 'image/svg+xml');

    dropFiles(screen.getByTestId('harness-cell'), [file]);

    expect(screen.queryAllByRole('img')).toHaveLength(0);
    expect(createObjectUrlSpy).not.toHaveBeenCalled();
    expect(rejectionOf('harness-cell')).toEqual({ reason: UNSUPPORTED_TYPE, rejecting: 'true' });
  });
});

describe('cell image transport and persistence tripwires', () => {
  let tripwires: SinkTripwire[] = [];

  beforeEach(() => {
    tripwires = installProhibitedSinkTripwires();
  });

  afterEach(() => {
    tripwires.forEach((tripwire) => {
      tripwire.restore();
    });
    tripwires = [];
  });

  it('crosses no transport or storage boundary across a complete drop, replace, dismiss and clear cycle', () => {
    const key = cellImageKey('ws-13', 0, 0);
    render(
      <CellImageProvider>
        <HarnessCell imageKey={key} />
        <StoreControl
          label="clear everything"
          onAct={(store: CellImageStore) => {
            store.clearAllCellImages();
          }}
        />
      </CellImageProvider>,
    );
    const cell = screen.getByTestId('harness-cell');

    // Exercise each transport-relevant mutation path: accept, replace, both refusal classes, dismiss,
    // and clear-all.
    dropFiles(cell, [realRasterFile('one-pixel.png', MINIMAL_PNG_BYTES, 'image/png')]);
    expect(screen.getByAltText('one-pixel.png')).toBeInTheDocument();

    dropFiles(cell, [rasterFile('second.png')]);
    dropFiles(cell, [textFile('notes.txt')]);
    dropFiles(cell, [oversizeRasterFile('huge.png')]);
    dropFiles(cell, [rasterFile('third.png')]);
    fireEvent.click(dismissControlFor('third.png'));
    dropFiles(cell, [rasterFile('fourth.png')]);
    fireEvent.click(screen.getByRole('button', { name: 'clear everything' }));

    expect(screen.queryAllByRole('img')).toHaveLength(0);
    expect(createObjectUrlSpy.mock.calls.length).toBeGreaterThan(0);
    expect(crossedSinks(tripwires)).toEqual([]);
  });

  it('crosses no boundary when the provider unmounts and sweeps what it still holds', () => {
    const key = cellImageKey('ws-13', 1, 0);
    const view = render(
      <CellImageProvider>
        <HarnessCell imageKey={key} />
      </CellImageProvider>,
    );

    dropFiles(screen.getByTestId('harness-cell'), [rasterFile('photo.png')]);
    view.unmount();

    // The URL sweep runs during unmount; it may revoke held URLs but must not cross any watched
    // transport or storage boundary.
    expect(revokedUrls()).toEqual([mintedUrl(1)]);
    expect(crossedSinks(tripwires)).toEqual([]);
  });

  it('instruments every boundary it claims to watch', () => {
    // Cross every promised tripwire deliberately so a missing installation cannot make the negative
    // tests pass vacuously; this covers method, prototype, namespace, and setter instrumentation.
    expect(crossedSinks(tripwires)).toEqual([]);

    // Each sink is reached by its assembled name, so a name that did not match the one the counter
    // was installed under would throw here rather than pass quietly.
    callWindowSink(SINK_TEXT.networkFetch, 'https://example.test/ping');
    const request = new XMLHttpRequest();
    request.open('GET', 'https://example.test/ping');
    request.send();
    navigator.sendBeacon('https://example.test/ping');
    callWindowSinkMethod(SINK_TEXT.localStore, 'setItem', 'probe', 'value');
    callWindowSinkMethod(SINK_TEXT.localStore, 'getItem', 'probe');
    callWindowSinkMethod(SINK_TEXT.localStore, 'removeItem', 'probe');
    callWindowSinkMethod(SINK_TEXT.localStore, 'clear');
    callWindowSinkMethod(SINK_TEXT.indexedDatabase, 'open', 'probe');
    document.cookie = 'probe=value';

    // Named, and in installation order, so a gap says which boundary went uninstrumented.
    expect(crossedSinks(tripwires)).toEqual([
      SINK_TEXT.networkFetch,
      'XMLHttpRequest.open',
      'XMLHttpRequest.send',
      'navigator.sendBeacon',
      'Storage.setItem',
      'Storage.getItem',
      'Storage.removeItem',
      'Storage.clear',
      `${SINK_TEXT.indexedDatabase}.open`,
      'document.cookie',
    ]);
    expect(tripwires).toHaveLength(10);
  });
});

describe('cell image source policy', () => {
  // The tests above prove the paths they exercise. This one reads every production module of the
  // feature from disk and scans it, which is what catches a forbidden sink introduced on a path no
  // test happens to drive — the mechanical form of the ephemerality and no-transport contracts.
  const featureSources = FEATURE_SOURCE_PATHS.map((relativePath) => ({
    relativePath,
    source: readClientSource(relativePath),
  }));

  it('reads every module the feature actually ships, so nothing escapes the scan', () => {
    const directoryModules = sourceFileSystem
      .readdirSync(sourcePathResolver.resolve(CLIENT_SOURCE_ROOT, 'features/cellImages'))
      .filter((entry) => entry.endsWith('.ts') || entry.endsWith('.tsx'))
      .map((entry) => `features/cellImages/${entry}`)
      .sort();

    expect(directoryModules).toEqual(
      FEATURE_SOURCE_PATHS.filter((path) => path.startsWith('features/')).sort(),
    );
    expect(featureSources.every((entry) => entry.source.length > 0)).toBe(true);
  });

  it('is armed with the identifiers it claims to scan for, so no typo can weaken it', () => {
    // Reassemble each fragmented identifier from a different split so a typo in either construction
    // is detected.
    expect(SINK_TEXT.networkFetch).toBe(identifierFrom('f', 'etc', 'h'));
    expect(SINK_TEXT.localStore).toBe(identifierFrom('lo', 'calSto', 'rage'));
    expect(SINK_TEXT.sessionStore).toBe(identifierFrom('sess', 'ionSto', 'rage'));
    expect(SINK_TEXT.indexedDatabase).toBe(identifierFrom('index', 'ed', 'DB'));
    expect(SINK_TEXT.httpClientPackage).toBe(identifierFrom('a', 'xi', 'os'));
    expect(SINK_TEXT.backendSdkPackage).toBe(identifierFrom('f', 'ireba', 'se'));
    expect(ROOT_ALIAS).toHaveLength(2);
    expect(ROOT_ALIAS.startsWith('@')).toBe(true);
    expect(ROOT_ALIAS.endsWith('/')).toBe(true);

    // Second, the scan is shown to be live: a line containing each real breach must be caught by the
    // pattern set. Without this the empty result below could mean "nothing forbidden is present" or
    // "nothing is being looked for", and those are not the same claim.
    const plantedBreaches = [
      `window.${SINK_TEXT.localStore}.setItem('k', 'v');`,
      `window.${SINK_TEXT.sessionStore}.setItem('k', 'v');`,
      `window.${SINK_TEXT.indexedDatabase}.open('db');`,
      `${SINK_TEXT.networkFetch}('/api/cells');`,
      `import client from '${SINK_TEXT.httpClientPackage}';`,
      `import app from '${SINK_TEXT.backendSdkPackage}/app';`,
      `import { store } from '${ROOT_ALIAS}store';`,
      'document.cookie = "k=v";',
      'const request = new XMLHttpRequest();',
      "navigator.sendBeacon('/api/cells');",
      "const socket = new WebSocket('wss://example.test');",
      'const copy = JSON.stringify(images);',
      'const reader = new FileReader();',
      'const bytes = await file.arrayBuffer();',
      'const focusable = <div tabIndex={0} />;',
      'const markup = <div dangerouslySetInnerHTML={html} />;',
      'const workbook = useAppSelector(selectWorkbook);',
    ];

    plantedBreaches.forEach((line) => {
      expect({ line, caught: PROHIBITED_SOURCE_PATTERNS.some(({ pattern }) => pattern.test(line)) })
        .toEqual({ line, caught: true });
    });
  });

  it('contains no persistence, transport, serialization, byte-read or asynchronous admission sink', () => {
    const breaches: string[] = [];

    featureSources.forEach(({ relativePath, source }) => {
      PROHIBITED_SOURCE_PATTERNS.forEach(({ label, pattern }) => {
        if (pattern.test(source)) {
          breaches.push(`${relativePath}: ${label}`);
        }
      });
    });

    expect(breaches).toEqual([]);
  });

  it('mints an object URL in exactly one place and releases it in the same module', () => {
    const storeSource = readClientSource('features/cellImages/cellImageStore.tsx');

    // One mint site in the whole feature, and it is the store's: a second one anywhere else would put
    // a blob outside the ownership registry that the four release paths sweep.
    featureSources.forEach(({ relativePath, source }) => {
      const mints = occurrences(source, /URL\s*\.\s*createObjectURL/g);
      expect({ relativePath, mints }).toEqual({
        relativePath,
        mints: relativePath.endsWith('cellImageStore.tsx') ? 1 : 0,
      });
    });

    // And the module that mints is the module that releases, so the pairing is auditable in one file.
    expect(occurrences(storeSource, /URL\s*\.\s*revokeObjectURL/g)).toBeGreaterThan(0);
  });

  it('validates a dropped file before it mints an object URL for it', () => {
    const storeSource = readClientSource('features/cellImages/cellImageStore.tsx');
    const refusalGate = storeSource.indexOf('const refusal = refusalFor(file)');
    const mintSite = storeSource.indexOf('URL.createObjectURL');

    // Ordering in the source, not merely in one observed run: the gate has to precede the mint site
    // for "a refused payload leaves no object URL minted" to hold for every payload rather than for
    // the handful the cases above happen to drive.
    expect(refusalGate).toBeGreaterThan(-1);
    expect(mintSite).toBeGreaterThan(refusalGate);
  });
});
