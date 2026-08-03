// Cell.tsx merges the drag affordance over the caller's own style object, and that merge is where a
// shorthand and one of that shorthand's own longhands can end up side by side. React's next update then
// clears the longhand the affordance contributed while the shorthand — identical across the two renders
// — is never re-emitted, so everything the shorthand declared is gone for the rest of the mount. No
// source-text assertion can see that, and neither can a harness that merely models Cell: it takes the
// real component, really rendered, really dragged over.
//
// The three root-alias specifiers Cell still imports are answered with stand-ins declared in this file
// alone. That is deliberately not a Jest module mapper: the production resolution failure those
// specifiers cause is left exactly as it is, and no other test file sees a substitute.

import { fireEvent, render, screen } from '@testing-library/react';
import type { CSSProperties } from 'react';
import Cell from '../../../components/Cell';
import { CellImageProvider } from '../cellImageStore';
import { CELL_IMAGE_TOKENS } from '../cellImageTokens';
import { cellImageKey } from '../cellImageKey';

// Babel hoists these above the imports above, so Cell resolves against them rather than failing to
// resolve at all. Each stands in for exactly what Cell reads from the module and nothing more.
jest.mock(
  '@/store',
  () => ({
    useAppSelector: () => undefined,
    useAppDispatch: () => () => undefined,
  }),
  { virtual: true },
);

jest.mock(
  '@/store/workbookSlice',
  () => ({
    updateCell: (payload: unknown) => ({ type: 'workbook/updateCell', payload }),
  }),
  { virtual: true },
);

jest.mock(
  '@/utils/cellFormatting',
  () => ({
    formatCellValue: (value: unknown) => String(value),
  }),
  { virtual: true },
);

const CELL_VALUE = 42;
const CELL_ID = 'A1';
const IMAGE_KEY = cellImageKey('sheet-1', 0, 0);

// Deliberately shorthand declarations of the three families an affordance writes into, plus one family
// it never touches. Hex and keyword forms are this file's own fixtures, not design values: the feature's
// own colours all come from the token module.
const CALLER_BACKGROUND = '#ffffff';
const CALLER_OUTLINE = '1px solid #dddddd';
const CALLER_TRANSITION = 'color 400ms';
const CALLER_BORDER = '1px solid #cccccc';

// jsdom rewrites some declaration values on assignment, so a token-built or fixture-built expectation is
// compared against itself pushed through the same normalisation, never against a literal.
const asDeclared = (property: string, value: string): string => {
  const probe = document.createElement('div');
  probe.style.setProperty(property, value);
  return probe.style.getPropertyValue(property);
};

const declaredValue = (node: HTMLElement, property: string): string =>
  node.style.getPropertyValue(property);

// The drag data store shape this feature reads: a file list plus the item kinds hover-phase acceptance
// is decided from. jsdom implements no DataTransfer to construct.
const dataTransferFor = (files: File[]) => ({
  files,
  items: files.map((file) => ({ kind: 'file', type: file.type })),
  types: ['Files'],
});

const rasterFile = (name: string): File =>
  new File(['cell-style-fixture-bytes'], name, { type: 'image/png' });

const textFile = (name: string): File => new File(['not-an-image'], name, { type: 'text/plain' });

// React only warns about a shorthand/longhand collision on an update, and it is the same check a browser
// runs — which makes it the one witness available here for the outline and transition families, whose
// shorthands jsdom does not expand into longhands at all.
const COLLISION_WARNING = /style property during rerender/;

let consoleErrorCalls: string[];

const collisionWarnings = (): string[] =>
  consoleErrorCalls.filter((entry) => COLLISION_WARNING.test(entry));

let createObjectUrlDescriptor: PropertyDescriptor | undefined;
let revokeObjectUrlDescriptor: PropertyDescriptor | undefined;
let revokeObjectUrlSpy: jest.Mock<void, [string]>;
let urlCounter = 0;

beforeEach(() => {
  consoleErrorCalls = [];
  jest.spyOn(console, 'error').mockImplementation((...args: unknown[]) => {
    consoleErrorCalls.push(args.map((arg) => String(arg)).join(' '));
  });

  urlCounter = 0;
  createObjectUrlDescriptor = Object.getOwnPropertyDescriptor(URL, 'createObjectURL');
  revokeObjectUrlDescriptor = Object.getOwnPropertyDescriptor(URL, 'revokeObjectURL');
  // Direct assignment rather than jest.spyOn: there is no property on jsdom's URL to spy on.
  URL.createObjectURL = jest.fn<string, [Blob | MediaSource]>(() => {
    urlCounter += 1;
    return `blob:cell-style-test/${urlCounter}`;
  });
  revokeObjectUrlSpy = jest.fn<void, [string]>();
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
  jest.restoreAllMocks();
});

// Matches the cell's own box rather than the value inside it, so the assertions read the element the
// affordance is actually applied to. A query is used rather than a container or a parent hop because the
// affordance belongs to whichever element carries the class, and that is what should be asserted on.
const isCellBox = (_content: string, element: Element | null): boolean =>
  element !== null && element.className === 'cell';

const mountCell = (style: CSSProperties): void => {
  render(
    <CellImageProvider>
      <Cell id={CELL_ID} value={CELL_VALUE} style={style} imageKey={IMAGE_KEY} />
    </CellImageProvider>,
  );
};

// React keeps the same element across every update, so each case captures it once and reuses it.
const cellBox = (): HTMLElement => screen.getByText(isCellBox);

// Enter then over, which is the pair a browser delivers before a drop and the pair that lights the
// affordance. Each fireEvent is its own task, so React commits the lit state before the leave arrives —
// exactly the two-commit sequence the defect needs and a single batched pair would hide.
const hoverWith = (node: HTMLElement, files: File[]): void => {
  const dataTransfer = dataTransferFor(files);
  fireEvent.dragEnter(node, { dataTransfer });
  fireEvent.dragOver(node, { dataTransfer });
};

const leaveWith = (node: HTMLElement, files: File[]): void => {
  fireEvent.dragLeave(node, { dataTransfer: dataTransferFor(files) });
};

const dropWith = (node: HTMLElement, files: File[]): void => {
  fireEvent.drop(node, { dataTransfer: dataTransferFor(files) });
};

describe('caller style survives the drag affordance on the real Cell', () => {
  it('gives the caller background shorthand back after a hover that was committed', () => {
    mountCell({ background: CALLER_BACKGROUND, width: 112, height: 72 });
    const cell = cellBox();
    const idleBackground = asDeclared('background-color', CALLER_BACKGROUND);
    expect(declaredValue(cell, 'background-color')).toBe(idleBackground);

    hoverWith(cell, [rasterFile('accepted.png')]);
    // The tint is what a hovered cell should be showing, and a longhand already won over the shorthand
    // it followed, so this is unchanged from the superseded merge.
    expect(declaredValue(cell, 'background-color')).toBe(
      asDeclared('background-color', CELL_IMAGE_TOKENS.dropActiveBackground),
    );

    leaveWith(cell, [rasterFile('accepted.png')]);
    // The regression: the affordance's longhand was cleared and the caller's shorthand, unchanged across
    // the two renders, was never re-emitted — so this read back empty and the cell stayed unpainted for
    // the rest of its mount.
    expect(declaredValue(cell, 'background-color')).toBe(idleBackground);
    expect(collisionWarnings()).toEqual([]);
  });

  it('gives it back on every subsequent hover, so the repair is not a single-cycle one', () => {
    mountCell({ background: CALLER_BACKGROUND, width: 112, height: 72 });
    const cell = cellBox();
    const idleBackground = asDeclared('background-color', CALLER_BACKGROUND);
    const files = [rasterFile('accepted.png')];

    hoverWith(cell, files);
    leaveWith(cell, files);
    hoverWith(cell, files);
    expect(declaredValue(cell, 'background-color')).toBe(
      asDeclared('background-color', CELL_IMAGE_TOKENS.dropActiveBackground),
    );

    leaveWith(cell, files);
    expect(declaredValue(cell, 'background-color')).toBe(idleBackground);
    hoverWith(cell, files);
    leaveWith(cell, files);
    expect(declaredValue(cell, 'background-color')).toBe(idleBackground);
    expect(collisionWarnings()).toEqual([]);
  });

  it('gives it back across a drop that lands while the affordance is lit', () => {
    mountCell({ background: CALLER_BACKGROUND, width: 112, height: 72 });
    const cell = cellBox();
    const idleBackground = asDeclared('background-color', CALLER_BACKGROUND);
    const files = [rasterFile('accepted.png')];

    hoverWith(cell, files);
    dropWith(cell, files);

    // One commit takes the cell from lit-and-empty to unlit-with-a-picture: the affordance's longhands
    // are withdrawn and the containing block arrives, both in the same update.
    expect(screen.getByRole('img', { name: 'accepted.png' })).toBeInTheDocument();
    expect(declaredValue(cell, 'position')).toBe('relative');
    expect(declaredValue(cell, 'background-color')).toBe(idleBackground);

    fireEvent.click(screen.getByRole('button', { name: 'Remove image accepted.png' }));

    // Back to the idle branch, which hands the caller's own object over untouched.
    expect(declaredValue(cell, 'position')).toBe('');
    expect(declaredValue(cell, 'background-color')).toBe(idleBackground);
    expect(revokeObjectUrlSpy).toHaveBeenCalledTimes(1);
    expect(collisionWarnings()).toEqual([]);
  });

  it('collides with no shorthand in the outline or transition families either', () => {
    // jsdom does not expand either shorthand into longhands, so the residue itself is invisible here;
    // React's collision check is not, and it is the same check that precedes the residue in a browser.
    mountCell({
      background: CALLER_BACKGROUND,
      outline: CALLER_OUTLINE,
      transition: CALLER_TRANSITION,
      width: 112,
      height: 72,
    });
    const cell = cellBox();
    const files = [rasterFile('accepted.png')];

    hoverWith(cell, files);
    leaveWith(cell, files);

    expect(collisionWarnings()).toEqual([]);
    expect(declaredValue(cell, 'outline')).toBe(asDeclared('outline', CALLER_OUTLINE));
    expect(declaredValue(cell, 'transition')).toBe(asDeclared('transition', CALLER_TRANSITION));
    expect(declaredValue(cell, 'background-color')).toBe(
      asDeclared('background-color', CALLER_BACKGROUND),
    );
  });

  it('keeps every affordance declaration intact while the drag is over the cell', () => {
    // The withdrawal must not cost the affordance anything: this is the state the experiment is meant to
    // show a user, and every value in it still comes from the token module.
    mountCell({ background: CALLER_BACKGROUND, width: 112, height: 72 });
    const cell = cellBox();

    hoverWith(cell, [rasterFile('accepted.png')]);

    expect(declaredValue(cell, 'outline-width')).toBe(
      asDeclared('outline-width', CELL_IMAGE_TOKENS.dropOutlineWidth),
    );
    expect(declaredValue(cell, 'outline-style')).toBe(
      asDeclared('outline-style', CELL_IMAGE_TOKENS.dropOutlineStyle),
    );
    expect(declaredValue(cell, 'outline-color')).toBe(
      asDeclared('outline-color', CELL_IMAGE_TOKENS.dropActiveOutlineColor),
    );
    expect(declaredValue(cell, 'background-color')).toBe(
      asDeclared('background-color', CELL_IMAGE_TOKENS.dropActiveBackground),
    );
    expect(declaredValue(cell, 'transition-duration')).toBe(
      asDeclared('transition-duration', CELL_IMAGE_TOKENS.transitionDuration),
    );
    expect(declaredValue(cell, 'transition-property')).toBe(
      asDeclared('transition-property', 'outline-color, outline-width, background-color'),
    );
  });

  it('leaves the caller background alone through a refusal, which writes no background longhand', () => {
    // The withdrawal is driven off the applied affordance's own keys, so a refusal — which tints
    // nothing — must not disturb a background the cell is still meant to be showing.
    mountCell({ background: CALLER_BACKGROUND, width: 112, height: 72 });
    const cell = cellBox();
    const refused = [textFile('notes.txt')];

    hoverWith(cell, refused);
    dropWith(cell, refused);

    expect(declaredValue(cell, 'outline-color')).toBe(
      asDeclared('outline-color', CELL_IMAGE_TOKENS.dropRejectOutlineColor),
    );
    expect(declaredValue(cell, 'background-color')).toBe(
      asDeclared('background-color', CALLER_BACKGROUND),
    );
    expect(collisionWarnings()).toEqual([]);
  });

  it('leaves shorthands outside every affordance family exactly as the caller declared them', () => {
    mountCell({ border: CALLER_BORDER, width: 112, height: 72 });
    const cell = cellBox();
    const declaredBorder = asDeclared('border', CALLER_BORDER);
    const files = [rasterFile('accepted.png')];

    hoverWith(cell, files);
    expect(declaredValue(cell, 'border')).toBe(declaredBorder);

    leaveWith(cell, files);
    expect(declaredValue(cell, 'border')).toBe(declaredBorder);
    expect(collisionWarnings()).toEqual([]);
  });

  it('gives a caller backgroundColor longhand back across the same cycle', () => {
    // The control: a caller that declared the longhand collides with nothing, and never did. It must
    // still come back, or the withdrawal would have reached a declaration it had no business touching.
    mountCell({ backgroundColor: CALLER_BACKGROUND, width: 112, height: 72 });
    const cell = cellBox();
    const idleBackground = asDeclared('background-color', CALLER_BACKGROUND);
    const files = [rasterFile('accepted.png')];

    hoverWith(cell, files);
    expect(declaredValue(cell, 'background-color')).toBe(
      asDeclared('background-color', CELL_IMAGE_TOKENS.dropActiveBackground),
    );

    leaveWith(cell, files);
    expect(declaredValue(cell, 'background-color')).toBe(idleBackground);
    expect(collisionWarnings()).toEqual([]);
  });

  it('reports no console error at all across a full hover cycle', () => {
    // The collision filter proves the specific defect is gone; this proves the repair did not trade it
    // for some other complaint from React, jsdom or the feature itself.
    mountCell({ background: CALLER_BACKGROUND, width: 112, height: 72 });
    const cell = cellBox();
    const files = [rasterFile('accepted.png')];

    hoverWith(cell, files);
    leaveWith(cell, files);

    expect(consoleErrorCalls).toEqual([]);
  });
});
