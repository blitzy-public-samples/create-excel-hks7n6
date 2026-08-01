# Cell Image Drop — Experiment Note

This note records an experimental capability added to the spreadsheet client: an image file dragged out of the operating system and dropped onto a cell renders inside that cell's bounds. The capability exists to answer a visual question, not to ship a feature. It is deliberately narrow, deliberately unpersisted, and deliberately additive — no existing behaviour of the workbook, the grid, the formula bar, the ribbon, the sidebar, or any server contract is altered by it. The note is self-contained, because no prior requirement or terminology in this folder concerns images, media, or drag-and-drop; everything it asserts is stated from the delivered implementation under `frontend/src/features/cellImages/` and `frontend/src/types/cellImage.ts`.

## 1. Purpose and Non-Goals

### 1.1 The question being asked

The stated goal is a purely visual assessment of the implications of uploading images into spreadsheet cells, as an experiment for future projects. The deliverable is therefore optimized for **observability of visual consequences** rather than for feature completeness: what matters is how much of a picture a default-sized cell can actually show, how the picture is clipped, whether the grid's geometry survives, whether a value underneath a picture stays legible, and whether a grid full of pictures still feels like a grid.

Anything that would make the prototype more complete without making those consequences more visible was left out on purpose. The sections below state exactly where the boundary was drawn, so a future team deciding whether to build this properly can see what was measured and what was never attempted.

### 1.2 What "upload" means here

"Upload" means **ingestion into the browser's memory**, and nothing more. A dropped file becomes a blob URL held in a page-scoped map and rendered through an `<img>` element. No HTTP request is issued, no endpoint was added, no database column or schema field exists for an image, and no migration accompanies this work. The absence of all of that is a designed property of the experiment, not an omission.

The repository already contains a Cloud Storage upload path at `backend/app/services/file_storage.py`. It is **deliberately left unused**. Reusing it would contradict the requirement that the images uploaded do not need to be saved anywhere, and it would put image bytes on a durable surface that this experiment has no reason to touch.

### 1.3 Non-goals

Each row below is a capability that was consciously not built. None is pending, scheduled, or partially present.

| Not built | Why it is out of scope |
|---|---|
| Server-side upload, or any HTTP transport of image bytes | Nothing is saved anywhere, so there is nothing to transport |
| Persistence of any kind — Cloud SQL, Firestore, Cloud Storage, `localStorage`, `sessionStorage`, IndexedDB, `redux-persist` | A page refresh is accepted as the end of an image's life |
| Images in CSV or XLSX import and export | Would require changing an existing serialization subsystem |
| Formula-engine awareness of images | Would require changing an existing evaluation subsystem |
| Real-time collaboration or cross-client synchronization of images | Requires a transport and a persisted representation, neither of which exists |
| Clipboard copy and paste of images | Would extend an existing interaction subsystem |
| Undo and redo for image placement or removal | Would extend an existing history subsystem |
| Excel-style floating-picture behaviour — resize, move, or anchor | Images are cell-bound; free placement is a different feature with different geometry |
| A click-to-upload `<input type="file">` fallback | The requested ingestion mechanism is a drag-and-drop operation |
| Cross-page and cross-tab image drags | These carry URL or markup strings rather than a file; see section 6.1 |
| Multi-image fan-out across neighbouring cells | Would need grid geometry and would change selection semantics |
| Image compression, thumbnail generation, EXIF handling, or SVG sanitization | Each is a processing pipeline, and none is needed to see how a picture looks in a cell |

## 2. How to Try It

### 2.1 The interaction

- Drag an image file from the operating system's file manager or desktop over any cell in the grid. While an acceptable payload is over the cell, the cell shows a two-pixel dashed blue outline and a faint blue background tint, and the cursor shows a **copy** affordance.
- Release the pointer to place the image. Because a dropped file is read and measured before anything is rendered, the picture appears on a slightly later tick than the drop rather than instantly.
- A small circular dismiss control sits at the top-right of a placed picture. Activating it removes the picture and reveals the cell's own value again.
- Drag-and-drop is the **only** ingestion mechanism. There is no file picker, no upload button, and no ribbon or sidebar control.

### 2.2 What a cell accepts

- Exactly one image per cell. Dropping onto a cell that already holds a picture replaces it.
- Dropping several files at once uses the **first acceptable image file** in the payload and ignores the rest.
- A payload that carries no file at all is a silent no-op: no cell is touched and nothing is announced, because nothing was done wrong.
- A file that is refused leaves the cell exactly as it was. The cell flashes a red dashed outline and a single application-level status notice states the reason in plain words, then retires itself after `rejectionNoticeMs` (2500 ms). Section 5 lists the eight reasons a file can be refused.

### 2.3 The intended manual assessment procedure

The procedure below is the one the experiment was designed around.

1. **Start the development server** from `frontend/` and open the Workbook route.
2. **Drop a PNG, then a JPEG**, dragged from the desktop onto several different cells — including a cell that already holds a value.
3. **Observe four things**, which are the four axes section 4 describes in detail:
   - how much of the image a default-sized cell can show, and how the remainder is clipped;
   - whether the aspect ratio is preserved as expected;
   - whether a value underneath a dropped image remains legible or is fully obscured;
   - whether scrolling the grid with images present feels different from scrolling a plain grid.

### 2.4 That procedure is currently blocked

The single-page application **cannot boot as delivered**, for reasons that predate this experiment entirely and are outside its scope to repair. `CI=true npm run build` fails on unresolvable import specifiers in existing source. The development server does start and does serve the page, but the application never mounts: the bundle's entry module throws on the first of those specifiers before it can render anything, so the page's `<div id="root">` stays empty and a compile-error overlay covers the viewport. The procedure in 2.3 therefore cannot be run today.

This is stated up front rather than buried, because it changes how the rest of this note should be read: the visual claims in section 4 describe what the implementation is built to do and what a reader should look for once the client boots, and the **verified** evidence for the feature's behaviour is the jsdom component suite. Section 7 records the inherited defects in full, together with the measurements taken against them.

## 3. The Ephemerality Contract

This contract is the mechanism by which two promises are made literally true rather than merely aspirational: that the images are not saved anywhere, and that no other functionality changes. It is binding on the implementation and is asserted directly by the test suite.

### 3.1 Shape

The whole of an image's state is one entry in one page-scoped map:

```text
Record<'{worksheetId}:{rowIndex}:{colIndex}', {
  objectUrl, fileName, mimeType, sizeBytes,
  pixelWidth, pixelHeight, decodedBytes, frameCount, droppedAt
}>
```

- The key is derived at the render site by `cellImageKey(worksheetId, rowIndex, colIndex)`, which returns `` `${worksheetId ?? 'ws'}:${rowIndex}:${colIndex}` ``. It is built from the same worksheet, row and column basis the grid already uses for its React key, and it deliberately never reads the worksheet's cell collection: that collection is typed as a record in the persisted view model while the grid iterates a nested row-and-cell array, so any key taken from it would be unstable.
- `droppedAt` is **epoch milliseconds** from `Date.now()`, not a `Date` object.
- `pixelWidth`, `pixelHeight`, `decodedBytes` and `frameCount` are the results of the pre-commit measurement described in section 5. They are recorded on the entry because an entry can only exist if those measurements were taken and passed — which makes "nothing is rendered that was not measured first" checkable by reading the type — and because the retention budget is accounted from `decodedBytes`.

### 3.2 Residency

The map lives in the feature provider's React context **only**. It is never placed in Redux, never in the workbook slice's `currentWorkbook`, never in the `Cell`, `Worksheet`, or `Workbook` shapes in `frontend/src/schema/workbookTypes.ts`, never in `localStorage`, `sessionStorage`, IndexedDB, or a cookie, never in a REST request or response body, and never in a Firestore document. It is never passed through `JSON.stringify`, and it is unobservable by any reducer under `frontend/src/store/`.

### 3.3 Object-URL lifecycle

An object URL is created at **exactly one moment**: after a file has passed every validation gate and the retention budget has room for it. Nothing is minted for a file that is refused, so a refusal allocates nothing that would later have to be released.

A created URL is revoked at exactly one of four moments:

| Release path | Trigger | What is released |
|---|---|---|
| (a) Replacement | The same key receives another image | The superseded URL, after the replacement is already in flight, so no render ever points at a revoked blob |
| (b) Explicit clear | The dismiss control on a placed picture | That cell's URL, leaving the cell's own value and formula untouched |
| (c) Clear-all | `clearAllCellImages()` | Every URL the store holds |
| (d) Provider unmount | The provider goes away | Everything still outstanding, swept from the store's live-URL registry |

**Every created URL therefore has exactly one matching revoke.** Two implementation details enforce it rather than merely intending it:

- Ownership is held in a ref that is written synchronously wherever a URL is minted or released, so every release path observes what is owned at the instant it runs rather than at the last render. A record read during a render would be stale for the same-task sequences that orphan a URL or revoke one twice.
- The store has a single release site, and it removes a URL from the live registry **before** revoking it. A second release of the same URL — a dismissal followed by an unmount in one React turn, for instance — finds nothing to remove and returns without revoking. Release is therefore idempotent, and the suite asserts exactly that.

Why the invariant matters: an unpaired object URL pins its blob for the document's lifetime. That leak would itself corrupt the assessment being conducted, because a grid that grows heavier the longer it is used cannot tell a reader anything reliable about how a grid with pictures behaves.

The reducer that holds the map is **pure** — it contains no `URL` call, no timestamp, no timer, and no event side effect. Creation and revocation live in the exposed callbacks and in the unmount effect, which is what makes the lifecycle auditable by reading one file. A measurement that resolves after its request stopped being current is abandoned rather than committed: each mutation of a key issues a monotonic claim, and a replacement, a clear, a clear-all, or a provider unmount invalidates the claim an in-flight measurement holds, so a superseded, cleared, or unmounted request mints nothing at all.

### 3.4 Lifetime boundary

The map survives component remounts and client-side route changes, because the provider sits above the router in the application shell — the chain is the Redux provider, then the auth provider, then the cell-image provider, then the router. It is destroyed by a hard refresh or by closing the tab. That is exactly the boundary the requirement asked for — a page refresh will lose them, and that is not a problem — and no more than that. Nothing was added to make images outlive a document, and nothing was added to shorten their life below a route change.

### 3.5 Why the state deliberately sits outside Redux

The `Cell` and `Worksheet` shapes in `frontend/src/schema/workbookTypes.ts` are the shapes that cross the REST boundary and feed the Firestore sync path. Adding an image field to either would silently change a persisted contract. Holding images in a parallel, non-persisted map is precisely what makes the non-regression promise literally true:

- `frontend/src/store/index.ts` gains no reducer.
- `frontend/src/store/workbookSlice.ts` gains no action, no reducer, and no selector.
- `frontend/src/schema/workbookTypes.ts` gains no field, so no serialized payload changes shape.
- No image value ever passes through Redux middleware, because the feature dispatches no Redux action on any code path.

The image also never displaces the cell's data. It renders as a layer visually covering the cell's value area while the cell's `value` and `formula` remain untouched in the store, and dismissing the image reveals the original value unchanged. The whole test suite runs with **no Redux provider in the tree**, which makes the feature's independence from the persisted model structural rather than asserted.

## 4. Observed Visual Implications the Experiment Exists to Surface

These four axes are the reason the prototype was built. Each states what the implementation does and what a reader should therefore look for.

### 4.1 Clipping versus row height

The picture is scaled with `object-fit: contain` inside a layer that fills the cell box and carries `overflow: hidden`. The layer is out of flow, so **row height and column width never change**: the cell that mounts a picture supplies a positioned containing block, and that declaration offsets nothing and grows no row or column.

This is the single most important visual property of the experiment, because it is what reveals how much of a picture a default-sized cell can actually show. A grid that silently grew its rows to fit pictures would answer a different and much easier question; a grid that holds its geometry forces the real one into view — at a default cell size, is a picture in a cell legible at all, or is it a coloured smudge that only becomes useful once rows and columns are resized?

### 4.2 Aspect-ratio behaviour

Contained scaling preserves the source ratio and letterboxes the picture inside the cell box rather than distorting it. A wide photograph in a tall cell leaves space above and below; a tall photograph in a wide cell leaves space either side. Nothing is stretched. What to watch for is how quickly that letterboxing dominates: the more a cell's aspect ratio differs from the picture's, the less of the cell the picture actually uses, and default spreadsheet cells are far wider than they are tall.

### 4.3 Legibility of a value underneath an image

The overlay composites **above** the cell's value text, and the value is not deleted. The question the experiment surfaces is therefore whether the value remains readable through or around the picture, or is fully obscured — and how a reader is supposed to tell that a cell with a picture still holds data at all. Dismissing the picture restores the value immediately and unchanged, which makes the comparison easy to make repeatedly on the same cell.

### 4.4 Perceived grid performance

Every accepted picture is a live blob rendered by the browser inside a scrolling container, so whether a grid with several pictures present scrolls differently from a plain grid is a judgement a reader has to make by scrolling it. The implementation removes the two confounds that would otherwise make that judgement meaningless: no object URL is ever leaked, so the page does not grow heavier the longer it is used, and a real change to the image map is what triggers a re-render — a transition that changes nothing returns the same state object, so a no-op costs no render anywhere in the grid.

Render scoping is nonetheless an accepted and recorded limitation rather than a solved problem: the context value carries the whole map and the single pending notice, so a real change re-renders every mounted cell rather than only the cell that changed. That is a deliberate trade, taken because key-scoped subscriptions would have changed a contract this experiment fixed.

### 4.5 Interaction-preservation properties worth observing

These properties make it possible to judge the visual consequences without a changed interaction model confusing the assessment.

- **Click-to-edit still works through the picture.** The overlay container is `pointer-events: none`, and only the dismiss control opts back into pointer events. Clicking the picture therefore reaches the cell root and opens the inline editor exactly as clicking an empty cell does. Activating the dismiss control stops the event from propagating before the removal runs, so removing a picture never opens the editor.
- **The picture is suppressed entirely while a cell is being edited**, so the auto-focused input is never obstructed by an image. The picture returns when editing ends.
- **Keyboard navigation is untouched.** The feature adds no `keydown` or `keyup` listener on `window` or `document`, and it adds no `tabIndex` inside a cell, so the grid's existing single tab stop and its window-level arrow-key navigation remain the whole keyboard model.

## 5. Security Posture, and Why SVG Is Excluded

### 5.1 The raster-only allow-list

A cell accepts five MIME types, and only these five:

- `image/png`
- `image/jpeg`
- `image/gif`
- `image/webp`
- `image/bmp`

**`image/svg+xml` is explicitly excluded.** It is absent from the allow-list, an SVG dropped on a cell is refused, and the suite asserts that refusal directly.

### 5.2 Why SVG is excluded

An SVG is an XML document parsed by the same engine that parses HTML. It can carry scripts, event-handler attributes, embedded HTML, and references to external resources. Rendering any image through an `<img>` element is materially safer than inlining untrusted markup, but the consistently recommended posture on an image-preview surface that has **no sanitizer** is not to accept the format at all.

The research basis for that decision, consulted while the feature was designed, is Fortinet's FortiGuard analysis of the SVG attack surface, practitioner guidance on cross-site scripting through SVG, and GitHub Security Advisory **GHSA-rcg8-g69v-x23j** against `makeplane/plane`, in which an SVG profile-image upload yielded cross-site scripting. Excluding the format costs the experiment nothing: a vector image tells a reader no more about how a picture looks inside a cell than a raster one does.

### 5.3 Why a declared type and a byte count are not enough

A dropped file offers two pieces of metadata that a page is tempted to trust, and neither describes what a decoder will do with the bytes.

- **`type` is a label, not a fact.** A vector image renamed to end in `.png` is handed to the page as `image/png`. An allow-list check on the label alone would therefore admit exactly the file class 5.2 excludes. Reading the container's own signature is what turns the label into a verified claim; a payload whose bytes do not match its declared type is refused as `format-mismatch`.
- **`size` is the compressed length.** Every raster container stores its canvas size in a header, so a few dozen bytes can legitimately declare a surface thousands of pixels on a side, which a decoder would then allocate in full. Bounding the **declared** surface before anything decodes it, and the **decoded** surface before anything is retained, is what closes that gap.

Validation therefore runs as an ordered pipeline, cheapest and most conclusive first, so that an expensive step is never reached by a payload a free check could have refused:

1. **Metadata** — the allow-list, a non-empty file, and the per-file byte ceiling. Nothing is read from the file at all.
2. **Container** — signature match plus declared canvas size and frame count, from one bounded, transient read. Formats that cannot animate are read only up to a 64 KiB header prefix; the animation-capable containers are read in full, because a frame count cannot be known from a prefix.
3. **Ceilings on the declared surface** — arithmetic on step 2.
4. **Decode** — the surface a real decoder reports. This is the only step that costs memory. Where the platform offers no decoder to ask, the container's own declaration is used instead.
5. **Ceilings again** — re-applied to the larger of the declared and the decoded measurement, because a container may under-declare its canvas and what has to be paid for is the surface actually produced.

Step 3 running before step 4 is the load-bearing detail: a file whose header declares an enormous canvas is refused **without ever being decoded**, so the expansion it was built to trigger never happens. Only a file whose declared surface already fits the ceilings is decoded at all.

### 5.4 The ceilings, and what each one bounds

| Limit | Value | What it prevents |
|---|---|---|
| Per-file bytes | 10 MiB | An arbitrarily large blob being retained in the map |
| Width and height | 4096 each | A surface that satisfies a pixel budget while breaking a layout |
| Pixels | 4 Mpx | The widely-demonstrated 4096 × 4096 expansion, refused before anything decodes it |
| Decoded bytes per image | 16 MiB, derived from the pixel ceiling at four bytes per pixel | A modest file asking a decoder for a large allocation |
| Frames per animation | 64 | An animation multiplying decode work while its canvas stays small |
| Retained images | 24 | An unbounded number of live blobs |
| Aggregate encoded bytes | 32 MiB | A total footprint no per-image bound can constrain |
| Aggregate decoded surface | 64 MiB | A total decode cost no per-image bound can constrain |

The per-file 10 MiB ceiling is checked against `File.size` **before any object URL is created**, and so is every other gate above, so a refused payload allocates nothing. The aggregate budgets are measured against what is retained at the moment of the check, with the entry being replaced credited back — so replacing a picture never charges twice for a slot it already holds. They are judged once as a preflight before anything is read, and again with the measured decoded cost immediately before the URL is minted, with nothing awaited in between.

Because these are eight distinct failure modes rather than one, a refusal reports which one it was. The reasons are `unsupported-type`, `too-large`, `format-mismatch`, `undecodable`, `dimensions-too-large`, `too-many-frames`, `too-many-images`, and `budget-exceeded`; each maps to a distinct plain-language phrase in the status notice, and each figure the notice quotes is derived from the constant that enforces it, so a limit and its explanation cannot disagree.

### 5.5 Properties that reinforce the posture

- **Nothing is persisted or re-served**, so stored cross-site scripting is structurally impossible: there is no durable copy of a dropped file for anyone else's browser to fetch.
- **The `blob:` URL is same-origin and lifetime-bound to the document**, and it is revoked on every release path in section 3.3.
- **The image is rendered only through `<img src>`** — never inlined, never inside an `<object>` or an `<iframe>`, and never through `dangerouslySetInnerHTML`.

### 5.6 Residual risk, and why an object URL rather than a data URL

Blob memory is bounded by the limits in 5.4 and by guaranteed revocation on every release path, but within those limits it is bounded by user behaviour: a reader who fills twenty-four cells with large pictures will hold what the budgets allow for as long as the page lives. That is the intended cost of the experiment, and it is why the aggregate budgets exist at all rather than only a per-file one.

`URL.createObjectURL` is used rather than `FileReader.readAsDataURL`. Base64 encoding inflates memory by roughly one third and the read is asynchronous — both of which would distort the very visual assessment being conducted, the first by exaggerating the memory cost of pictures in cells and the second by adding latency that has nothing to do with rendering.

This design also deviates deliberately from the common documented example, which revokes an object URL inside the image's `load` handler. That pattern is wrong here: the same URL must stay valid across React re-renders, so revocation is deferred to the four release paths in section 3.3 instead. One temporary URL is minted inside the decode fallback of step 4 above, and it is revoked on every outcome including a timeout, so a measurement can never pin a blob either.

## 6. Known Limitations

### 6.1 Ingestion and behaviour

- **Only file-system drags are supported.** Dragging an image out of another web page or another tab delivers `text/uri-list` and `text/html` strings rather than a file. Honouring those would require a network fetch constrained by cross-origin rules, which is well outside a lightweight experiment. Such a payload advertises no file, so the drop effect is reported as `none` and the drop itself is a silent no-op.
- **Images are cell-bound and non-interactive apart from removal.** They cannot be resized, moved, or anchored, and there is no floating-picture mode.
- **No fan-out.** Only the first acceptable file in a multi-file drop is used; the rest are ignored rather than distributed across neighbouring cells.
- **Nothing survives a refresh**, by design. See section 3.4.
- **No ribbon button and no sidebar control were added.** The Insert tab remains the placeholder it already was.
- **A picture appears on a later tick than the drop**, because the file is read, measured, and budgeted before anything is rendered. This is a direct and accepted consequence of the validation in section 5.
- **A stray drop is guarded, not routed.** A file dropped anywhere in the document that is not a cell is cancelled so the browser does not navigate away to display it, but it is not adopted by any cell either. The guard subscribes only to `dragover` and `drop` on `window`, stays in the bubble phase, never stops propagation, and cancels the default only when the payload actually advertises a file — so a dragged link or a dragged text selection keeps its normal browser behaviour, and the grid's own window key listener is untouched.

### 6.2 The styling substrate

The client ships **zero** stylesheets. No `.css` or `.scss` file exists anywhere in it, and `frontend/src/index.tsx` imports `@/styles/index.css`, a stylesheet that does not exist. Tailwind CSS is declared as a dependency at `^3.2.7` and is installed, but it is **entirely unwired**: there is no `tailwind.config.js`, no `postcss.config.js`, and no `@tailwind` directive anywhere in the repository. There is consequently no theme, no palette, and no token source of any kind to inherit from.

The planning documents in this folder record a different intention, and that intention must not be mistaken for the current state. `documentation/Technical Specifications.md` states at L26 that the interface is "Styled using Tailwind CSS for consistent and customizable design", names Tailwind CSS in the frontend stack row at L67 and in the frameworks table at L533, states at L405 that the interface "will be built using React and styled with Tailwind CSS", and states at L510 that the design "will follow Microsoft's Fluent Design System guidelines while leveraging Tailwind CSS". All five are **declared design intent that is not implemented**.

Wiring Tailwind was therefore rejected rather than overlooked: introducing a global stylesheet and a PostCSS pipeline would restyle every existing component, which is exactly the "changing other functionality" this work was told not to do. Instead the feature carries its own styling in a **feature-local frozen token module**, `frontend/src/features/cellImages/cellImageTokens.ts`, consumed as inline style objects. Twelve presentation values live in its frozen token object — the drop-active outline colour and background tint, the rejection outline colour, the status-strip surface and text colours, the outline width and style, the dismiss control's size and inset, the overlay stacking value, the affordance transition duration, and the notice lifetime — and it is frozen so that a component cannot mutate a shared design value. The same module also carries the allow-list and the resource limits from section 5, on the principle that a design value and a policy limit are both decisions that must not be written inline at a usage site. No global stylesheet is added, no class is added to any existing element, and no existing class name's meaning changes. The colour values are Tailwind 3 defaults, so a future decision to wire Tailwind would be a one-for-one substitution rather than a rewrite.

### 6.3 Accessibility posture

The client is otherwise unstyled, so **no claim of WCAG conformance is made here**. What the feature does is avoid moving the client further from the Level AA goal that `documentation/Software Requirements Specifications (SRS).md` states at L568, "Accessibility compliance with WCAG 2.1 Level AA standards":

- The `<img>` carries the dropped file's name as its `alt` text, so a picture is never an unlabelled graphic.
- The removal control is a real `<button type="button">` with an `aria-label` naming the file it removes. No `div` masquerades as an interactive control, and the visible glyph inside the button is marked decorative so it does not compete with that label.
- The control draws its own hover, pressed, and focus treatment as an inset ring, because the overlay's clipping would eat a conventional outline. Focus outranks pointer state, so a keyboard user can always see where they are.
- There is exactly **one** application-level `role="status"` region with `aria-live="polite"`, rendered only while a refusal notice is present. It is announced politely rather than assertively and takes no focus.
- The grid's existing `role="grid"` and `role="row"` structure is preserved, and no `tabIndex` is added inside a cell, so the grid's single tab stop and its arrow-key navigation remain the keyboard model unchanged.

## 7. Pre-Existing Defects That Block Live In-Browser Assessment

Everything in this section predates this experiment, was inherited by it, and is **reported rather than repaired** — repairing any of it would exceed a lightweight addition that changes no other functionality. None of it is a regression caused by this feature, and nothing here is promised, scheduled, or implied to be fixed by this work.

### 7.1 The client cannot boot as delivered

`CI=true npm run build` fails. Webpack cannot resolve the `@/…` import specifiers that existing source files use throughout — the build stops at `Can't resolve '@/styles/index.css'` from `frontend/src/index.tsx`. The cause is that `frontend/tsconfig.json` declares only `@components/*`, `@utils/*`, `@hooks/*`, `@services/*`, and `@types/*` at L11–L17, so there is no `@/*` alias at all, and Create React App 5 honours `baseUrl` but not `paths` in any case. **This failure exists identically before and after this work and must not be read as a regression**; its message and failure mode were confirmed unchanged.

The development server exhibits the same defect, and it is worth recording precisely what a browser sees, because it explains why no amount of work on this feature would make the manual procedure runnable. The server starts, answers the root request with the page, and even emits and serves a bundle. It reports three unresolved specifiers from `frontend/src/index.tsx` — `@/app`, `@/store`, and `@/styles/index.css` — for each of which webpack substitutes a stub that throws at module-evaluation time. The application's entry module therefore dies on the first one, with `Uncaught Error: Cannot find module '@/app'`, before the render call it would otherwise reach. Observed in a real browser: `<div id="root">` exists but is empty, the document body has no text, and none of the application's own elements is present anywhere in the page — no shell container, no ribbon, no `role="grid"`, no row, no cell. The served bundle in fact contains only `src/index.tsx`, because module traversal stopped at the first broken edge, so no component of this feature ever reaches the browser at all. A dev-server compile-error overlay covers the viewport in its place, and the state is deterministic across reloads.

### 7.2 The type-safety baseline

TypeScript strict-mode checking is the one quality gate in this repository that genuinely functions, so it carries the weight of the non-regression claim. Measured with a temporary configuration that extends `frontend/tsconfig.json` and empties `compilerOptions.types` — required because a hoisted `@types/node` in the installed tree uses syntax the project's TypeScript 4.9 cannot parse, and `skipLibCheck` does not suppress syntax errors — the baseline is:

- **86 diagnostics in total**, spread across 20 client source files, of which 54 are "cannot find module" arising from the alias mismatch in 7.1.
- Per-file counts for the three components this work touched are unchanged: `Grid.tsx` **9**, `Cell.tsx` **5**, `app.tsx` **5**.
- **Zero** diagnostics originate from any file this feature created.

The non-regression bar for this work was therefore "introduce zero new errors", not "make the project compile", and the measurement above is what confirms it was met. Linting agrees: `eslint --no-fix` across `src/` reports 8 warnings and 0 errors, all of them pre-existing unused declarations in existing files, and none in a file this feature created.

### 7.3 The single toolchain exception

`react-scripts` was invoked by four declared scripts in `frontend/package.json` yet appeared in neither dependency block, so `npm run build`, `npm run lint`, and `npm test` could not execute at all as delivered. Restoring it as a devDependency pinned to **`5.0.1`** is the **single** exception this change set makes to repairing nothing. It is justified on three grounds: the manifest already committed to it in four places, it contributes no code to the application runtime and changes no existing component's rendered output, and it is the only way the new test suite can execute. Exactly one line was added to the manifest; the scripts, the ESLint configuration, and the browser targets were not touched.

### 7.4 Other inherited defects, reported not repaired

- The grid passes `value`, `isSelected`, `onClick`, and `onChange` to a cell whose prop contract declares `id`, `value`, and `style`. The prop this feature added is optional and rides alongside that existing disagreement without reconciling it.
- `formatCellValue` is called with one argument against a two-parameter signature.
- Several bindings are imported from modules that do not export them, among them `selectActiveWorksheet`, `useAppSelector`, `useAppDispatch`, `Provider`, and `AuthProvider`.
- `@/styles/index.css` is imported and does not exist; the `frontend/src/components/index.ts` barrel is imported and does not exist.
- `frontend/src/index.tsx` uses the legacy `ReactDOM.render` entry point rather than the React 18 root API, and mounts a second Redux provider around an application that already provides one.
- Several packages are imported but declared nowhere: `@reduxjs/toolkit`, `react-router-dom`, `firebase`, `mathjs`, and `date-fns`.
- There is no committed lockfile, no `.gitignore`, and no `.env.example`.
- `.github/workflows/ci.yml` runs a `type-check` script that the manifest does not define, and runs `npm ci` and `npm test` at the repository root and in `backend/`, where no `package.json` exists.
- `.github/workflows/cd.yml` triggers on a workflow named "Continuous Integration" while `.github/workflows/ci.yml` is named "CI", so the deployment workflow can never fire.
- `README.md` describes a Node and Express backend with MongoDB and Styled-components styling, when the actual backend is Python and FastAPI and Styled-components is installed in neither dependency block; it also links `./API.md`, `./CONTRIBUTING.md`, and `./LICENSE.md`, none of which exists.

### 7.5 The jsdom suite is the primary verification vehicle

Because 7.1 blocks the manual procedure, the component suite is where this feature is proven. It lives at `frontend/src/features/cellImages/__tests__/cellImages.test.tsx` and runs from `frontend/` with:

```bash
CI=true npm test -- --watchAll=false --ci
```

**Measured result: one suite, 49 tests, all passing.** Three properties make the suite able to run at all and worth trusting:

- It imports **only** the new modules, by relative specifier. It deliberately does not import the cell or grid components, because those reach the store and the formatting helper through the unresolvable `@/…` prefix from 7.1 and would stop the whole suite from loading. Local harnesses consume exactly what those components consume instead.
- It mounts **no Redux provider anywhere**, which turns the independence claim in section 3.5 into a structural fact.
- jsdom implements neither of the object-URL APIs nor a bitmap decoder, so all three are stubbed per test with deterministic identifiers. That constraint becomes an asset: minting and revocation are directly countable, which is what lets the one-revoke-per-create invariant be measured rather than assumed. The container fixtures, by contrast, are real bytes rather than mocks, because acceptance depends on a container's own signature, declared canvas, and frame count.

What the 49 tests cover, grouped as they are in the file:

| Group | What it establishes |
|---|---|
| Drop acceptance and refusal | One contained image and exactly one minted URL for an accepted raster drop; refusal with no URL minted for a non-image, an oversized file, a file whose contents contradict its declared type, a header declaring an enormous canvas, an over-long animation, a file the decoder refuses, and a file whose decoded surface exceeds the ceiling; dismissal releasing exactly the URL that was minted; replacement releasing only the superseded URL; and inert, non-throwing behaviour with no provider mounted |
| Ownership under batched mutation | Exactly one mint when a cell is set twice in one task; the held URL released and the in-flight drop abandoned when a cell is set and then cleared, or cleared entirely, in one task; no double release when a cell is cleared twice; nothing minted when the provider unmounts mid-measurement; and every held URL released on unmount and on clear-all |
| Resource budgets | Refusal once the retained-image count is full while still allowing a replacement; a slot freed by a dismissal; and refusal of drops that would exceed the aggregate encoded-byte and decoded-surface budgets |
| Window guard | A stray file drop cancelled anywhere in the document while other drags are left alone, and subscription to only the two drag events, never a key press |
| Decode probe fallbacks | Measurement by image element when no bitmap decoder exists, the probe URL always released including on failure, and fallback to the container's own declaration when no decoder is available at all |
| Editing interaction | The picture suppressed while the cell is edited and restored afterwards |
| Drag affordance and drop-effect signalling | `dragenter`, `dragover`, and `drop` all cancelled and a copy effect advertised for a file payload; no drop effect and no affordance for a payload with no file; the affordance held steady across nested enter and leave pairs, clamped at zero and reset on drop; the first acceptable image taken from a multi-file drop; a file-less drop treated as a silent no-op; and complete inertness for a cell rendered without an image key |
| Refusal echo and notice | An SVG refused by the raster-only allow-list; a scriptable format refused when handed straight to the store and not only when dropped; only the cell whose own drop was refused outlined; and the notice retired after its token lifetime with a later refusal given a full one |
| Provider lifetime | A picture kept through a child remount and discarded only with the provider |
| Idempotent release within one React turn | Released once and not twice when one turn clears a cell and unmounts the provider, and when one turn replaces a picture and unmounts |
| Render scoping | Broadcast to every mounted cell on a real change, but not on a no-op |
| Overlay layer and removal control | The picture clipped inside an inert layer filling the whole cell box; the picture scaled inside the cell box instead of resizing the cell; a real labelled button that stays clickable inside the inert layer; its hover, pressed, and focus treatment drawn as an unclippable inset ring; and a keyboard press on the control not removing the picture |
| Addressing | A distinct key derived for every worksheet, row, and column, and a picture staying addressed by its own key while a neighbour stays empty |

Once the inherited module-resolution and type defects in 7.1 and 7.2 are repaired in separate work, the manual procedure in section 2.3 becomes runnable with **no change to this feature**.
