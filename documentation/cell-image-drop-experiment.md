# Cell Image Drop — Experiment Note

An experimental, deliberately ephemeral prototype that lets an image file be dragged from the
operating system and dropped onto a single spreadsheet cell, where it renders inside that cell's
bounds. Nothing is uploaded, nothing is stored, and a page refresh discards everything.

This note is the write-up for that experiment: what it is for, how to try it, exactly what it holds
in memory and for how long, the visual questions it was built to answer, why it refuses SVG, what it
knowingly does not do, and which inherited defects currently stand between it and a live browser.

---

## 1. Purpose and non-goals

### Purpose

The goal is **purely visual assessment of the implications of putting images into spreadsheet
cells**, as groundwork for future projects. Concretely, it exists to make four questions answerable
by looking rather than by speculating:

- How much of a picture is still legible once the whole of it is scaled down to fit a default-sized
  cell?
- Is the aspect ratio preserved in a way that reads as correct, or does contained scaling reduce tall
  images to unusable slivers?
- Does a value or formula result underneath a picture stay legible, or is it fully obscured?
- Does a grid holding several pictures *feel* different to scroll and edit?

Every design decision below is subordinate to those questions. Where a choice made the visual
consequences easier to observe, it won; where a choice would have added feature surface without
changing what you can see, it was cut.

### Non-goals

This is not a picture feature for a spreadsheet. It is an instrument for looking at one. The
following are explicitly **not** built, and their absence is intentional rather than unfinished:

| Not built | Why |
|-----------|-----|
| Any server round-trip or upload endpoint | The images "do not need to be saved anywhere" |
| Persistence of any kind — database, object storage, `localStorage`, `sessionStorage`, IndexedDB | A page refresh is allowed to lose everything |
| Images in CSV or XLSX import and export | Would require changing an existing subsystem |
| Formula-engine awareness of images | Same |
| Clipboard copy and paste of images | Same |
| Undo and redo for placement or removal | Same |
| Real-time collaboration or cross-client sync of images | Same |
| Excel-style floating pictures — resize, move, anchor | Beyond a lightweight experiment |
| A click-to-upload file picker fallback | The brief specifies a drag-and-drop operation |
| Image compression, thumbnails, EXIF handling, SVG sanitization | Not needed to answer the four questions |

The overriding constraint was that this be a **lightweight addition without changing any other
functionality**. That is why the images live in their own store rather than in the workbook model,
why no Redux reducer or action was added, why no stylesheet was introduced, and why no persisted
type gained a field. See §3.

---

## 2. How to try it

The feature is reached through the normal grid. There is no ribbon button, no menu item, and no
sidebar control to find — that is the point of a drag-and-drop ingestion path.

1. Start the client:

   ```bash
   cd frontend
   npm install
   BROWSER=none PORT=3000 npm start
   ```

2. Open the workbook route and drag a PNG or JPEG from the desktop over the grid.
3. **While the pointer is over a cell**, that cell draws a dashed blue outline and a faint blue
   tint, and the cursor shows a copy affordance. That is the drop target telling you it will accept
   the payload.
4. Release. The picture appears inside that cell, in the same frame as the drop.
5. A small round control sits at the picture's top-right corner from the moment the picture appears —
   it is always there, not revealed by hovering. Hovering it, pressing it, or giving it keyboard focus
   draws a ring inside it so the target is unmistakable. Click it to remove the picture and reveal the
   cell's original value, unchanged. Note that **`Tab` does not reach it**: the control is deliberately
   outside the sequential tab order so that a grid full of pictures still has the one tab stop it has
   today. It is a real button all the same — screen readers find it by its name, and once it holds
   focus `Enter` and `Space` activate it. The trade-off is set out in §6.
6. Drop a second image onto the same cell to replace the first.
7. Try a text file, an SVG, a zero-byte file, or an image larger than 10 MiB. The cell flashes a
   **red** dashed outline and a single notice appears at the bottom of the window naming the file and
   the reason it was refused. No cell is modified, and a refused payload leaves no object URL minted
   and no blob-backed image resource retained.
8. Select several files at once and drop them together. Exactly one picture lands: the first file in
   the selection that is genuinely acceptable — right type, not empty, within the ceiling. An
   oversized or empty file at the front of the selection is passed over rather than allowed to spoil
   the drop, and there is no fan-out into neighbouring cells.
9. Refresh the page. Every picture is gone. That is the designed behaviour, not a bug.

> **Before you try this, read §7.** The single-page application does not currently boot, for several
> independent reasons that predate this experiment — no one of which is the whole cause. Until all of
> them are repaired, steps 2 onward cannot be performed in a browser, and the automated component
> suite is the working substitute.

### What to click if you only have the test suite

```bash
cd frontend
CI=true npm test -- --watchAll=false --ci
```

The suite exercises this feature's own pipeline in jsdom — the store, the drop hook and the overlay:
acceptance, both refusal paths, replacement, removal, bulk release, the window-level guard, editing
suppression, drag affordance and drop-effect signalling, and render scoping. It drives them through
small local harness components rather than through `Cell` and `Grid`, which it deliberately does not
import. The production wiring is not left unguarded either: a further group of cases reads
`components/Cell.tsx`, `components/Grid.tsx` and `app.tsx` from disk as text and fails if the drag
handlers, the affordance merge, the key-scoped subscription, the not-editing gate, the grid's derived
key or the provider's placement above the router is removed or miswired (§7). It runs with **no Redux
provider mounted**, which is how the feature's independence from the workbook store is proven rather
than asserted.

---

## 3. The ephemerality contract

This is the part of the design that makes both "nothing is saved" and "nothing else changed"
literally true rather than aspirational. It is a contract, not an implementation detail.

### Shape

One flat map, held in a React context provider mounted above the router. These are the types the
feature actually exports, as declared in `frontend/src/types/cellImage.ts` — the field comments below
are annotations for this note, not part of the source:

```ts
export type CellImageKey = string;

export interface CellImageEntry {
  objectUrl: string;   // a blob: URL minted from the dropped File
  fileName: string;    // used verbatim as the image's alternative text
  mimeType: string;    // the file's declared type, after the allow-list accepted it
  sizeBytes: number;   // the encoded length reported by File.size
  droppedAt: number;   // epoch milliseconds from Date.now()
}

export type CellImageMap = Record<CellImageKey, CellImageEntry>;
```

`CellImageKey` is a plain string alias, so the `{worksheetId}:{rowIndex}:{colIndex}` shape of a key is
a convention the type documents rather than one it enforces. What enforces it is that every key comes
from one pure function, `cellImageKey(worksheetId, rowIndex, colIndex)`, called at the render site
with the worksheet id and the row and column indices. That function deliberately does **not** read the
worksheet's cell collection, because the collection is modelled inconsistently in this repository —
typed as a record in one place and iterated as nested arrays in another — so anything keyed on it
would be unstable.

### Residency — where the map is *not*

The map lives in the provider's context and nowhere else. It is never placed in:

- the Redux store, or any slice, reducer, action, or selector;
- `Cell`, `Worksheet`, or `Workbook` in `frontend/src/schema/workbookTypes.ts` — the workbook model
  that `store/workbookSlice.ts` is typed against, and therefore the shape a saved workbook is built
  from;
- `WorkbookSchema`, `WorksheetSchema`, or `CellSchema` — the backend schema types that
  `services/api.ts` and `services/collaboration.ts` are typed against, and therefore the shapes that
  actually travel over REST and into the Firestore subscription;
- `localStorage`, `sessionStorage`, IndexedDB, or a cookie;
- any request or response body;
- any database row, document, or storage object.

Both type families are named separately because they are two distinct boundaries in this repository,
and the feature adds a field to neither. It is never serialized, and no **Redux** reducer in the
application can observe it — the only reducer that ever sees the map is the feature's own, held in the
provider's `useReducer` and unreachable from anything under `frontend/src/store/`. Because no shape on
either boundary gained a field, no payload anywhere changed shape, which is what lets the
non-regression claim be checked by inspection rather than only by testing.

A dropped picture also never touches the cell it covers. `value` and `formula` are left exactly as
they were, which is why removing a picture reveals the original content unchanged.

### Object-URL lifecycle — exactly one release per creation

A `blob:` URL pins its underlying blob for the lifetime of the document. An unreleased URL is
therefore a memory leak — and a leak would corrupt the very "does the grid feel slower" observation
this experiment exists to make. So the lifecycle is an invariant, not a best effort.

A URL is created at **exactly one** moment: after the dropped file has passed every admission gate
(§5). It is released at **exactly one** of four moments:

| # | Release path | Trigger |
|---|--------------|---------|
| a | Replacement | The same cell receives another picture; the superseded URL is released |
| b | Explicit clear | The dismiss control is activated |
| c | Clear-all | Every held URL is released |
| d | Provider unmount | A sweep releases everything still outstanding |

Release is **idempotent**: a URL is removed from the live set before it is revoked, so a second
release of the same URL finds nothing and returns without revoking. That matters because a dismissal
and an unmount can land in the same React turn.

The test suite asserts this invariant directly — it stubs `URL.createObjectURL` and
`URL.revokeObjectURL`, which jsdom does not implement, and counts the calls.

### Lifetime boundary

| Event | Pictures survive? |
|-------|-------------------|
| Component remount | Yes |
| Client-side route change | Yes — the provider sits above the router |
| Hard refresh (F5) | **No** |
| Tab close | **No** |

That boundary is exactly what was asked for: the state must be robust enough to navigate around
while assessing it, and must not outlive the page.

---

## 4. What the experiment is designed to surface

These are the observations the prototype exists to produce. Each records the mechanism that
determines the outcome, so that what you see can be attributed rather than guessed at.

### Clipping versus row height — the picture is contained, not cropped

**The cell never grows.** The picture is rendered in a layer pinned to all four edges of the cell,
out of normal flow, with `overflow: hidden`. Row height and column width are therefore identical
with and without a picture — nothing reflows, and the grid's geometry is not disturbed by a drop.

Two mechanisms are easy to conflate here, so they are stated separately. `object-fit: contain` scales
the **whole** picture down until all of it fits inside the cell box, so nothing is ever cropped and
nothing is stretched. `overflow: hidden` on the layer clips only accidental overflow, and it resizes
nothing. The cell clips; the picture loses none of itself to that clip.

This is the single most important property for the assessment, because it is what makes the real
question visible: *a spreadsheet cell is small, and the whole picture has to live within it.* The
scale is `min(boxWidth / imageWidth, boxHeight / imageHeight)`, and the smaller ratio binds. At the
proportions a spreadsheet conventionally uses — a box of about 80 by 20 CSS pixels — a 1920×1080
photograph is bound by the height ratio, `20 / 1080`, and so lands at roughly **1.9%** of its linear
size, about 36 by 20 CSS pixels. The width ratio of 4.2% never applies, because a conventional cell is
far wider than it is tall relative to a landscape photograph.

The box in that arithmetic is the cell's **padding** box, because that is what the layer is pinned to,
so any border or padding the cell carries comes off those numbers first. Measuring the real overlay in
a browser makes the difference concrete: in an 80 by 20 `border-box` cell with a 1px border, the
available box is 78 by 18, the same photograph renders at 32 by 18 CSS pixels, and the scale is 1.67%
rather than 1.9%. That box was read off the real overlay rather than assumed: a cell declared 80 by 20
with `box-sizing: border-box` and a 1px border reports a content box of exactly 78 by 18, and a 640×200
source dropped into it renders at 57.6 by 18 — a scale of 9.00%, which matches `min(78/640, 18/200)`,
with the 20.4 pixels of leftover width split evenly into about 10.2 pixels of empty cell on each side.
Either way the conclusion is the same and it is the one worth taking away — the observation to make is
not whether anything is cropped, but **how little of the picture remains legible at under two percent
of its linear size**, and therefore whether a picture in a default-sized cell communicates anything at
all. Widening the column or heightening the row is the natural next thing to try.

Note when reproducing this: **this repository ships no CSS at all**, and `Grid` does not currently
pass a `style` prop down to `Cell`, so cells have no author-supplied dimensions and render at
whatever a bare `div` gives them. Both are pre-existing conditions unrelated to this feature (§7).
The containment mechanism above is what matters and is independent of the numbers — whatever box the
cell ends up with, the picture is bounded by it and the box does not grow.

The alternative — letting a picture grow its row — was rejected precisely because it would have hidden
this finding behind a layout change.

### Aspect-ratio behaviour

The image is sized with `max-width: 100%`, `max-height: 100%` and `object-fit: contain`, centred in
the cell box. Consequences to observe:

- The aspect ratio is **always preserved**. Nothing is stretched, and nothing is cropped: the
  horizontal and vertical scale factors are the same number.
- The leftover space sits inside the **cell**, around the picture, never inside the picture itself.
- Whichever ratio is smaller binds, so it is the picture's aspect *relative to the cell's* that
  decides. Because a conventional cell is far wider than it is tall, an ordinary photograph — portrait
  **or** landscape — is bound by cell height and leaves space to its left and right; a portrait one
  ends up a narrow sliver, and only an image wider in aspect than the cell itself, such as a
  panorama, is bound by cell width. Measured in an 80 by 20 cell: a 1920×1080 landscape renders 32 by
  18 with space either side, and a 400×1200 portrait renders 6 by 18 — narrower than its own removal
  control. Dropping one of each side by side makes the asymmetry immediate.
- An image smaller than the cell is **not** scaled up, so it sits at its natural size with space
  around it. Small icons therefore behave quite differently from photographs — worth noting for any
  future design, since icon-in-cell and photo-in-cell are effectively different features.

### Legibility of a value underneath a picture

The picture is a layer **over** the cell's value area; the value is never removed. The layer is
opaque wherever the image is opaque, and it is centred, so how much of the value survives depends
entirely on how wide the contained picture ends up. A picture that fills the cell hides the value
completely. A height-bound one covers only the middle: measured in an 80 by 20 cell, a 1920×1080
photograph renders 32 pixels wide and leaves the leading characters of `1234.5` readable while
covering the rest — legible, but arbitrarily truncated, which is worse than either extreme because it
cannot be relied on.

The finding this is meant to force is a design question rather than a technical one: **a cell has one
visual slot, and a picture takes it.** Any future feature has to decide explicitly whether a picture
replaces the value, sits beside it in a widened cell, or floats above the grid the way Excel's
pictures do. Dismissing the picture restores the value instantly and unchanged, which makes the
before-and-after easy to compare.

Also worth watching: transparent PNGs let the value show through, which reads as an accident rather
than a feature, and is an argument for an explicit backdrop in any real implementation.

### Perceived grid performance

Two deliberate choices keep work off the drop path. A third property is often mistaken for a third
such choice, so it is stated for exactly what it guarantees and no more:

1. **The display source is a `blob:` URL from `URL.createObjectURL`, minted synchronously.** The
   rejected alternative, `FileReader.readAsDataURL`, inflates memory by roughly a third through
   base64 encoding and is asynchronous — both of which would have distorted the assessment. The
   picture is committed inside the drop event itself; nothing is read, parsed, or decoded first.
2. **A drop re-renders one cell, not the grid.** Each cell subscribes under its own key and reads
   back its own slice, so a picture dropped on one cell — or a refusal raised for one — leaves every
   other mounted cell unrendered; the suite measures this rather than asserting it. A drop therefore
   costs O(1) renders. What it does cost O(cells) in is *snapshot comparisons*: the provider notifies
   every subscriber on every publish, and each one compares its own slice to conclude that nothing
   concerning it moved. A comparison is far cheaper than a render, but it is not free and it scales
   with the number of mounted cells.
3. **An idle cell forwards the caller's own style object by identity.** With no picture and no drag
   in progress, the style object handed to the cell is the very object handed back to the DOM. That
   is an identity guarantee about styling, not a claim that the feature costs a mounted cell nothing.

**The prototype is not free for a grid that holds no pictures.** Every rendered cell pays a fixed
overhead whether or not it ever receives an image: four drag handlers on its root element, and four
store subscriptions — the cell reads the store once for its own picture and the drop hook reads it
again for that same cell's refusal, and each read installs two subscriptions — plus a drag-depth ref
and a drag-active state. That cost is proportional to the number of *mounted cells*, not to the
number of pictures, so a large grid pays it while the experiment is completely idle.

Two costs are therefore in play whenever the grid feels slower, and neither should be folded into the
other. The browser's own is decoded frames held for every visible picture, compositing during scroll,
and animated GIFs, which keep advancing frames while on screen. The prototype's own is the per-cell
overhead above. Scrolling a grid with a dozen pictures and then the same grid with none isolates the
first, because the second is present in both.

---

## 5. Security posture, and why SVG is excluded

The prototype renders a file that arrived from outside the application, so it takes a position on
input handling even though nothing is stored or re-served.

### Raster-only allow-list

Accepted: **PNG, JPEG, GIF, WebP, BMP**. Everything else is refused.

`image/svg+xml` is **deliberately absent.** An SVG is an XML document parsed by the same engine as
HTML, and it can carry scripts, event-handler attributes, embedded HTML, and external references.
Rendering through `<img>` is materially safer than inlining SVG markup, but the consistently
recommended posture for an image-preview surface with no sanitizer is not to accept SVG at all.
GitHub advisory **GHSA-rcg8-g69v-x23j** records exactly this class of problem — SVG profile-image
upload yielding cross-site scripting — which is why the risk is treated as concrete rather than
theoretical. Excluding SVG removes the only script-capable image class at **zero cost to the visual
assessment**, since none of the four questions in §1 needs vectors to be answered.

### What reinforces that decision

- **Nothing is persisted or re-served**, so stored cross-site scripting is structurally impossible.
- **The `blob:` URL is same-origin and lifetime-bound to the document**, and is revoked on every
  release path (§3).
- **The image is rendered only through `<img src>`.** Never inlined, never through
  `dangerouslySetInnerHTML`, never inside `<object>` or `<iframe>`.
- **Validation precedes the mint.** Every admission gate completes before any object URL exists, so a
  refused payload leaves no object URL minted and no blob-backed image resource retained. The suite
  asserts that a refused drop calls `createObjectURL` zero times. Read that narrowly: the dropped
  `File` is already resident by the time any of this code runs, and a refusal does record a small
  rejection so the notice can name the file. What a refusal cannot do is add a picture's worth of
  retained bytes.

### The three admission gates

A dropped file is judged on metadata alone, in this order, and **every gate completes before an
object URL exists** — so a refused payload leaves no object URL minted and no blob-backed image
resource retained, which the suite asserts by counting `createObjectURL` calls on the refusal paths:

| Gate | Check | Refusal |
|------|-------|---------|
| Declared type | `File.type` is one of the five raster types | `unsupported-type` |
| Non-zero length | `File.size > 0` | `unsupported-type`, with a notice saying the file is empty |
| Byte ceiling | `File.size <= 10 MiB` | `too-large` |

The middle gate exists because a payload of no length cannot be a picture whatever it claims to be,
and admitting one would leave a broken image in the cell plus an object URL pinning a blob that can
never be shown. Length is observable without reading a byte; a container signature is not.

### What the gates bound, and what they do not

This section is deliberately explicit, because a guard that is described more broadly than it is
implemented is worse than no guard at all.

**What is bounded.** The *declared* type of what will be rendered, and the *encoded* length of each
individual file. Nothing else.

**What is not bounded, with the worst case spelled out:**

- **Decoded surface.** The ceiling says nothing about how much memory a browser allocates to *display*
  a picture. A highly compressible 16,384 × 16,384 PNG fits comfortably under 10 MiB and asks for
  16,384 × 16,384 × 4 bytes = **exactly 1 GiB** of RGBA surface when it is decoded. Script cannot observe a user
  agent's decoded-frame cache, so any figure this prototype derived from declared dimensions would be
  an estimate presented as a bound — and reading dimensions at all means decoding, which means an
  asynchronous drop path.
- **Animation cost.** A small GIF or WebP can carry thousands of frames and consume sustained decode
  and compositing CPU for as long as it is on screen. Frame count is likewise a property of the bytes,
  not of the metadata.
- **Truncated or malformed content.** A half-written file of an accepted type and a non-zero length is
  admitted and renders broken. Telling it from a whole one requires reading its header.
- **Aggregate retention.** The ceiling is **per file**. There is no cap on how many pictures are held
  at once, no aggregate byte budget, and no eviction: dropping near-ceiling images into a hundred
  cells retains on the order of **1 GiB** of encoded blobs before any decoded surface is counted, and
  the map lives for as long as the document does.

**Why the prototype stops here.** Every one of those checks needs the file's bytes, and reading bytes
makes ingestion asynchronous. The plan of record for this experiment fixes ingestion as
**synchronous and zero-copy** precisely so that nothing sits between the drop and the paint — the
alternative was considered and rejected on the grounds that main-thread work inside the drop would
measure the prototype instead of the spreadsheet, distorting the very perceived-performance question
§4 exists to answer. It also fixes the refusal vocabulary at two values and the resource policy at a
per-file ceiling, and it records unbounded aggregate retention as a **knowingly accepted risk whose
mitigation is exactly the per-file ceiling plus guaranteed revocation on every release path** (§3).
An earlier iteration of this prototype did implement signature, dimension, decoded-size, frame-count
and aggregate-budget admission; it was removed because it expanded that frozen contract, introduced
asynchronous completions that could commit after newer intent, and did duplicate decode work in the
drop path. Restoring it is the right move for a product and the wrong move for this instrument.

**What the exposure actually is.** The payload arrives only when the person at the keyboard drags
their own file into their own tab. Nothing is uploaded, shared, persisted, or re-served, so no other
user and no later session can be affected, and there is no attacker-controlled path into the
pipeline — the blast radius is the experimenter's own tab, and the failure mode is resource pressure
rather than code execution. That pressure is, in part, one of the things §4 asks you to look at. It is
still a real limit, which is why it is written down here rather than left to be discovered.

**Working within it.** Dismiss pictures you are finished with — each dismissal revokes its blob
immediately — and reload the page to reclaim everything at once, since nothing survives a refresh.
Prefer normally-sized photographs over synthetic maximum-dimension images unless straining the
decoder is the specific thing you are trying to observe.

**What a production implementation would add**, in the order the value arrives: a bounded
container-signature check on the first few hundred bytes; dimension, total-pixel and frame-count
ceilings; decode-then-admit with a timeout so a picture that cannot be displayed is never retained;
a retained-count and aggregate-byte budget that credits the cell being replaced; and a decode-error
path that releases the blob and clears the cell. All five need asynchronous byte access and a wider
refusal vocabulary than two values — which is exactly the trade this prototype makes in the other
direction.

### Toolchain exception: the development-only Create React App dependency

One dependency was added for this work: **`react-scripts@5.0.1`**, as a `devDependency`. Four scripts
in `frontend/package.json` already invoked it — `start`, `build`, `test`, `eject` — and the ESLint
configuration the manifest extends (`react-app`) ships inside it, so without the declaration the
repository's own build, lint and test commands could not run at all and the suite that verifies this
feature could not execute. It contributes **no application runtime code** and changes no rendered
output.

It nonetheless carries a security cost that is recorded here as an explicit, scoped exception rather
than left implicit. **Verified on 1 August 2026** in this repository's installed tree:

| Measurement | Result |
|-------------|--------|
| `npm audit` (development tree) | **28** advisories — 14 high, 5 moderate, 9 low |
| `npm audit --omit=dev` (what ships to a user) | **0** advisories |
| Where the 28 come from | every one is transitive through `react-scripts`, which is the only declared dependency that reaches any vulnerable install. The clusters are the development server (`webpack-dev-server` → `sockjs`), the SVG/CSS pipeline (`@svgr/webpack` → `svgo` → `css-select` → `nth-check`, and `postcss@7.0.39` nested under `resolve-url-loader`), the bundling helpers (`serialize-javascript` under both `css-minimizer-webpack-plugin` and `workbox-webpack-plugin` → `workbox-build` → `rollup-plugin-terser`), and the Jest stack (`jest-environment-jsdom` → `jsdom` → `http-proxy-agent`, and `bfj` → `jsonpath` → `underscore`). `npm audit` itself names `react-scripts` as the only fix available |
| Upstream status | The React team deprecated Create React App on 14 February 2025, citing the absence of active maintainers, and put it into maintenance mode. `react-scripts` still publishes `latest: 5.0.1` — the version pinned here — with only `5.1.0-next.*` pre-releases beyond it, so there is no patched *stable* release to move up to. Note that `react-scripts@5.0.1` itself carries no npm `deprecated` flag; the deprecation is of the project, not of this package version |

**The conditions of the exception**, all of which are properties of how the tool is used rather than
of the package:

- The affected code paths are the **development server and the build**, never the browser a user
  visits: the production dependency tree audits clean.
- **Never expose the development server.** Bind it to localhost only, do not tunnel or port-forward it,
  and do not browse untrusted sites while it is running — the `webpack-dev-server` advisories in this
  chain are exploited by a malicious page probing a predictable local origin.
- **Treat build inputs as trusted.** The SVG, CSS and bundling advisories in this chain are reached by
  processing hostile assets, so do not add third-party SVG or CSS to this repository while it stands.
- **Do not run the development server or the build in shared or privileged CI** without isolating the
  job.

**Why the exception is not simply resolved here.** Migrating to a maintained build stack, adding
`overrides`/`resolutions`, or committing a lockfile are all changes to the manifest and the build that
the plan of record for this experiment explicitly places outside its scope — it pins this exact
version, states that nothing else in the manifest changes, records CRA's deprecation and transitive
weight as an accepted risk, and puts the absent lockfile in the inherited-defect list (§7). Doing any
of them under cover of an image prototype would be precisely the "changing other functionality" this
work was told not to do.

**What lifts it.** Migrating the client off Create React App — to Vite or to a maintained CRA
successor — as its own piece of work, with the lockfile that migration deserves. That work also
removes the module-resolution blocker in §7, so the two belong together.

### Non-file drags

Dragging an image out of *another web page* delivers `text/uri-list` and `text/html` strings rather
than a `File`. Honouring those would require a CORS-constrained network fetch, so such drags are
treated as unacceptable payloads: the cursor shows `none` and the drop is a silent no-op. Only
file-system drags are supported.

### Stray drops

A browser handles a dropped file by default — opening or downloading it — **even when the drop lands
outside any registered target.** A drop that missed a cell by a few pixels would therefore navigate
the browser away from the application and end the assessment mid-session. The provider registers a
window-level `dragover`/`drop` guard that cancels that default, but **only** for payloads that
actually advertise a file, so dragged links and dragged text keep their normal browser behaviour. The
guard never stops propagation, so cell handlers still receive their events, and it observes only
those two event names, so it cannot interfere with the grid's keyboard navigation.

---

## 6. Known limitations

Each of these is a conscious boundary. Where a limitation follows from a frozen contract, that is
stated, because the right fix in a real implementation would be to widen the contract rather than to
work around it here.

- **A truncated file that declares an accepted type is accepted and renders broken.** Validation
  reads a file's *declared* type and its *length* — never its bytes — so a half-written PNG passes
  every gate and produces a broken image in the cell. The one content fact a metadata-only gate can
  honestly establish is that a payload of **zero** length cannot be a picture, and that case **is**
  refused: an empty file claiming `image/png` is turned away before any object URL is minted for it,
  and the notice says it is empty rather than that its format is unsupported. Anything beyond that —
  telling a truncated PNG from a whole one — requires reading bytes, which this prototype
  deliberately does not do (§5, *What the gates bound, and what they do not*). In a real
  implementation this is where container-signature validation would belong.
- **Only file-system drags work.** Cross-page and cross-tab image drags are a silent no-op (§5).
- **One picture per drop.** The drop handler takes the first file in the selection that satisfies
  **every** admission rule — declared type on the allow-list, non-zero length, within the 10 MiB
  ceiling — and ignores every other file. So a selection led by an oversized or empty picture does
  not shadow a smaller, perfectly acceptable one behind it; the leading file is simply passed over.
  If no file in the selection is acceptable, the first one that at least claims an allow-listed type
  is handed to the store anyway, purely so that the refusal can name that file and say precisely what
  was wrong with it. Acceptance itself stays decided in exactly one place: the hook chooses, the store
  admits. There is no fan-out across neighbouring cells, because that would require grid-geometry
  logic and would change selection semantics.
- **A drag carrying files but no allow-listed type at all is refused, not ignored.** The notice names
  the first file, which can read oddly when a mixed selection is dropped.
- **Pictures are cell-bound and inert.** They cannot be resized, moved, or anchored, and they are not
  selectable. The only interaction is removal.
- **Removal is a pointer action; `Tab` does not reach the control.** A native `<button>` is tabbable by
  default, so leaving that implicit would have added *one tab stop per picture* inside a grid whose
  keyboard model is a single stop plus arrow keys — and this experiment is not permitted to change that
  model. The control therefore carries `tabIndex={-1}`. It remains a real button in every other
  respect: it keeps its accessible name (`Remove image <file name>`), assistive technology reaches it
  and reports it, it is programmatically focusable, and `Enter` and `Space` activate it once it holds
  focus — which a pointer press gives it. This was measured in real Chrome, with the shipped overlay
  mounted in an isolated grid rather than through §2's blocked procedure: with two pictures present,
  real `Tab` presses moved straight through the grid container to the next control outside it and never
  entered a cell, while clicking a control still removed exactly its own picture. The honest reading is
  that this trades keyboard *discoverability* of removal for an unchanged grid keyboard model, and it
  is a defensible trade only because ingestion is pointer-only to begin with: there is no click-to-upload
  fallback (§1, *Non-goals*), so a keyboard-only user cannot place a picture either. A real
  implementation should not copy this. It should give the grid a roving-tabindex model in which the
  focused cell exposes its own controls, or bind removal to a key on the selected cell — both of which
  need changes to the grid and cell components that this experiment is explicitly barred from making.
- **A picture is suppressed while its cell is being edited**, so the inline editor is never
  obstructed. It reappears when editing ends. This is deliberate, but it does mean a picture cannot
  be seen and its value edited at the same time.
- **The drop affordance animates in but clears instantly.** The transition is declared alongside the
  affordance, so the outline grows and the tint fades in over the token duration; it is deliberately
  absent from the idle style, because an idle cell must forward the caller's own style object by
  identity. Idle byte-identity was judged more valuable than a symmetric exit animation.
- **No stylesheet, so all styling is inline.** This repository ships no CSS at all and has Tailwind
  installed but entirely unwired. The feature's **design** values are centralized in one frozen token
  module — its five palette colours, the drop outline's width and style, the removal control's size and
  inset, the overlay's stacking value, the affordance transition duration and the refusal notice's
  lifetime — using Tailwind 3 defaults, so that wiring Tailwind later is a one-for-one substitution
  rather than a rewrite. That module also carries the two validation constants: the MIME allow-list and
  the byte ceiling. What it deliberately does **not** carry is the structural CSS around those values.
  The flex centring that positions the picture, `overflow: hidden`, `object-fit: contain`, the
  `max-width` and `max-height` of `100%`, `padding: 0`, `border: none`, the fully round border radius,
  `cursor: pointer`, the `pointer-events` values, and the transitioned property names are all written
  inline in the components, because each is layout or behaviour mechanics rather than a themeable
  decision. The accurate claim is therefore narrower than "nothing is hardcoded": no *design* value is
  hardcoded at a usage site. A further consequence of having no stylesheet is that the feature cannot
  use pseudo-classes, so the removal control's hover, pressed, and focus treatments are held in React
  state and drawn as an inset ring instead.
- **Nothing survives a refresh.** By design (§3).
- **Memory is bounded only by the per-file ceiling and by how many pictures you drop.** There is no
  global cap and no eviction, and the encoded ceiling says nothing about decoded surface. Guaranteed
  revocation on every release path is what keeps that manageable. The worst cases, the reason the
  prototype stops where it does, and how to work within it are set out in §5, *What the gates bound,
  and what they do not*.
- **One build-toolchain dependency carries known development-tree advisories.** `react-scripts@5.0.1`
  was declared so the repository's own build, lint and test scripts could run; it ships no runtime
  code and the production dependency tree audits clean, but the development server and build do carry
  advisories. The scoped exception, its conditions, and what lifts it are in §5, *Toolchain
  exception*.

---

## 7. Inherited defects that block live browser assessment

This must be stated plainly rather than worked around: **the single-page application does not boot as
delivered, for reasons that predate this experiment.** The manual procedure in §2 is therefore not
currently performable, and repairing the causes is explicitly outside this work.

There are **54 unresolved-module diagnostics, and they do not share one cause.** They fall into three
groups with three different repairs, so the table below attributes each group rather than blaming the
import alias for all of them.

| Defect | Consequence |
|--------|-------------|
| Existing source imports through an `@/…` prefix that no declared path alias covers — `tsconfig.json` declares `@components/*`, `@utils/*`, `@hooks/*`, `@services/*` and `@types/*`, but never `@/*` | **37** of the 54 unresolved-module diagnostics |
| Five packages are imported but declared in neither dependency block: `@reduxjs/toolkit`, `react-router-dom`, `firebase`, `date-fns`, `mathjs` | A further **11**, across six distinct specifiers. These need an installation decision, not an alias |
| Two modules are imported by repository-root-relative path — `backend/app/schema/workbook_schema` and `frontend/src/schema/workbookTypes` — which resolve under no `baseUrl` | The remaining **6**. These need the specifiers themselves rewritten |
| `src/components/index.ts` and `src/pages/index.ts` do not exist, yet `@/components` and `@/pages` are imported as if they were barrels | **9 of the 37 above would still fail** after a correct `@/*` alias, because the alias would resolve to directories that have no entry point |
| Create React App 5 honours `baseUrl` but not `paths` | Repairing the alias in `tsconfig.json` would settle type diagnostics without making the bundler resolve the same specifiers |
| `src/styles/` does not exist, yet `index.tsx` imports `@/styles/index.css` | Produces **no type diagnostic at all** — TypeScript does not check a CSS import — but it is the first thing the bundler fails on, so the build stops here before reaching anything above |
| 86 pre-existing TypeScript diagnostics across all **20** original client source files | The project does not typecheck as delivered |
| `Grid` and `Cell` disagree about their prop contract | Pre-existing mismatch, unrelated to images |
| Several imported bindings are never exported by the modules named | Includes the `useAppSelector`/`useAppDispatch` hooks components import |

The three groups add to the total exactly — 37 plus 11 plus 6 — and **no single one of them unblocks
the browser.** Correcting the alias leaves 17 diagnostics standing and 9 of its own 37 unfixed for
want of two barrel files; installing the packages touches neither the alias nor the barrels; and none
of the three does anything about the missing stylesheet, which is what the bundler actually stops on.
That is why §2's procedure cannot be unblocked by a single change, and why this note makes no estimate
of when it will be.

`CI=true npm run build` **still fails, identically, before and after this feature**, with
`Module not found: Error: Can't resolve '@/styles/index.css'`. Confirming that this mode and message
are unchanged is itself part of this work's validation. It must not be read as a regression.

### What this means for verification

Because the browser path is blocked, the **jsdom component suite is the primary verification
vehicle**. It was built to survive these defects: it imports only this feature's own modules, by
relative path, and never touches `Cell.tsx` or `Grid.tsx`, whose unresolvable specifiers would fail
at test time. In their place it renders small harness components that consume exactly what those two
components consume — `useCellImages`, `useCellImageDrop` and `CellImageOverlay`. It also stubs the
object-URL APIs jsdom does not implement, which turns that constraint into an asset: revocation
becomes directly assertable.

The measurable guarantees the suite provides in place of a browser, every one of them observed through
those harnesses:

- an accepted raster drop renders exactly one image whose alternative text is the file's name, and
  mints exactly one object URL;
- a refused drop renders no image and mints **zero** object URLs, proving validation precedes the
  mint — and the store's source is checked to place the gate before the mint site, so the ordering
  holds for every payload rather than only the tested ones;
- removal and replacement each release exactly one URL, and release is idempotent;
- a mutation for one cell key re-renders that cell and **not** its neighbour;
- the pipeline runs with no Redux provider in the tree;
- two fixtures are **genuine one-pixel rasters** — a real PNG and a real GIF, written out byte by byte
  — and the object URL is minted from the very `File` that was dropped, so the zero-copy claim is
  checked against identity rather than assumed;
- multi-file selection takes the first **fully acceptable** picture, proven from both directions:
  an oversized or empty leading file is passed over for a good one behind it, a good leading file is
  kept when a later one is unacceptable, and a selection of nothing but oversized rasters is refused
  with the size reason naming the first of them;
- **nothing is transmitted and nothing is persisted**, asserted rather than assumed: counters are
  installed over ten platform boundaries — `fetch`, `XMLHttpRequest.open`/`send`,
  `navigator.sendBeacon`, the four `Storage` methods, `indexedDB.open` and the `document.cookie`
  setter — and every one of them is still at zero after a complete accept, replace, refuse-on-type,
  refuse-on-length, dismiss, clear-all and provider-unmount cycle. A companion test then crosses **all
  ten** deliberately and asserts each one recorded it, so their silence cannot be the silence of
  instrumentation that never installed;
- **no forbidden sink exists anywhere in the feature's own source**, not merely on the paths the tests
  drive: every production module is read from disk and scanned for persistence, transport,
  serialization, byte-read, asynchronous-admission, root-alias-import, Redux-binding and unsafe-markup
  patterns, the module list is checked against the directory so a new file cannot escape the scan, and
  the single object-URL mint site is confirmed to live in the same module that releases it.

What the suite does **not** do is execute `Cell.tsx`, `Grid.tsx` and `app.tsx` themselves. Their own
contribution — the four drag handlers spread onto the cell root, the affordance merged over the
caller's style, the key-scoped subscription, the not-editing gate that suppresses the overlay, the idle
style object forwarded by identity, the key derived and passed down by the grid, and the provider
mounted above the router — is instead pinned by reading those three files from disk as text and
asserting the exact wiring. Deleting the handler spread, unscoping the subscription, bypassing the edit
gate, dropping the grid's prop, hardcoding a value the token module owns, or moving the provider below
the router each fail a test rather than passing silently. No Jest module name mapper is added to make
those files importable, because that would also hide the inherited resolution failure this section
documents. So the arrangement is pinned as text and the behaviour is covered by the harnesses, while
the two components' own runtime execution stays out of reach until the defects above are repaired.

### Why the list above is necessary but not sufficient

It would be convenient to write that repairing the five things named above — the missing `@/*` alias,
the five undeclared packages, the two root-relative specifiers, the two absent barrel files and the
absent stylesheet — makes §2 performable. It would also be untrue, so this note does not say it.
Suppose every one of the 54 unresolved-module diagnostics were settled tomorrow. What remains is still
enough to keep the page blank:

| What still stands | How many | Why it blocks a browser, not merely a typecheck |
|-------------------|----------|--------------------------------------------------|
| Implicitly-typed parameters (`TS7006`) in `Grid.tsx`, `Sidebar.tsx`, `Workbook.tsx`, `collaboration.ts`, `store/index.ts`, `userSlice.ts`, `workbookSlice.ts` | **21** | `strict` and `noImplicitAny` are both on, and Create React App fails a production build on a type error rather than warning past it |
| Unused declarations (`TS6133`, `TS6192`) in `Cell.tsx`, `ChartDialog.tsx`, `Ribbon.tsx`, `Dashboard.tsx`, `api.ts`, `workbookTypes.ts` | **7** | Same reason: `noUnusedLocals` and `noUnusedParameters` are on too |
| A default export imported as a named one (`TS2614`), both in `store/index.ts` | **2** | The two reducers are imported under names the slices never export, so the store cannot be composed at all |
| `app.tsx` uses the react-router **v5** API — `<Switch>` and `component={…}` | — | Which is why "install the five undeclared packages" is not even a well-defined repair: installing the current `react-router-dom` would leave this file broken, because v6 removed both |
| `Grid` and `Cell` disagree about their prop contract | — | The grid passes `value`/`isSelected`/`onClick`/`onChange`; `CellProps` declares `id`/`value`/`style` |
| `index.tsx` renders through the legacy `ReactDOM.render` and wraps a **second** Redux `<Provider>` around an app that already has one | — | Neither is a diagnostic, and neither is fatal alone, but both are part of why a served page stays empty |
| Bindings that are imported but never exported — including the `useAppSelector` and `useAppDispatch` hooks most components call on their first line | — | These surface inside the counts above and among the unresolved-module errors rather than as a class of their own |

Counted against this repository's own `tsconfig.json` that is **30** remaining diagnostics. Counted
with the `types`-emptied configuration this work's validation recipe uses, it is **32** — the two extra
being `Cannot find name 'process'` in `services/api.ts` and `store/index.ts`, which an installed
`@types/node` resolves and the override removes. The discrepancy is worth naming rather than glossing,
but it changes nothing that matters here: neither number is zero.

So the honest test of whether §2 has become performable is not the absence of any particular list of
defects. It is two observations, in this order: `CI=true npm run build` completes successfully, and a
browser loading the served application mounts the workbook route rather than a blank document. Until
both hold, §2 stays blocked, and this note offers no estimate of when that will be.

What *can* be said, and is worth saying, is the narrower half of the original claim: **when those two
observations do hold, this feature needs no change for §2 to work.** Every module it adds already
compiles without a diagnostic of its own, and every one is covered by the suite described above. That
is a claim about this feature's readiness, not a prediction about the application's, and it is the only
one this section can make and keep. Each repair listed here is a separate piece of work, and each is
outside this experiment's scope.

---

## Where the code lives

| Path | Role |
|------|------|
| `frontend/src/types/cellImage.ts` | The feature's TypeScript contracts |
| `frontend/src/features/cellImages/cellImageTokens.ts` | The design tokens — palette, outline, control sizing, stacking, timings — plus the MIME allow-list and the byte ceiling |
| `frontend/src/features/cellImages/cellImageKey.ts` | Pure key derivation |
| `frontend/src/features/cellImages/cellImageStore.tsx` | The ephemeral store, object-URL lifecycle, window guard, status notice |
| `frontend/src/features/cellImages/useCellImageDrop.ts` | The drag handler set, validation, drag-depth tracking |
| `frontend/src/features/cellImages/CellImageOverlay.tsx` | The in-cell picture layer and its removal control |
| `frontend/src/features/cellImages/__tests__/cellImages.test.tsx` | The suite described in §7 |
| `frontend/src/components/Cell.tsx` | Drop target, affordance, overlay mount (additive edits only) |
| `frontend/src/components/Grid.tsx` | Derives each cell's key (one added prop) |
| `frontend/src/app.tsx` | Mounts the provider above the router (one wrap) |
