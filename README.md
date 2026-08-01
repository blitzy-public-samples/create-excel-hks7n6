# Microsoft Excel Clone

A web-based clone of Microsoft Excel with essential spreadsheet functionalities.

## Features

- Create, edit, and save spreadsheets
- Basic cell formatting (bold, italic, underline, cell color)
- Formula support for basic arithmetic operations
- Data import/export (CSV)
- Responsive design for desktop and mobile use

### Experimental: images in cells

Drag an image file from your file system onto a cell and it renders inside that cell's bounds. This is
an experimental prototype rather than a supported feature, and its purpose is purely
**visual assessment** for future projects: how much of a picture a default-sized cell can show and how
it clips, whether the aspect ratio survives, whether a value underneath stays legible, and whether a
grid holding several pictures still feels responsive.

- **Drag-and-drop is the only way in.** Ingestion is exclusively the HTML5 drag-and-drop API — there is
  no click-to-upload file picker and no upload button.
- **Nothing is uploaded and nothing is persisted.** Each picture is held in browser memory only, as a
  `blob:` object URL. No HTTP request is issued, no backend endpoint exists for it, and nothing is
  written to a database, to Cloud Storage, to Firestore, to `localStorage`, `sessionStorage` or
  IndexedDB, or into any saved or serialized workbook.
- **Ephemerality is the design.** Pictures survive navigating between routes in the same page session
  and are lost on a page refresh or a tab close, which is explicitly acceptable for this experiment.
- **Cell data is untouched.** The picture is a layer drawn over the cell's value area; the cell's
  `value` and `formula` are never modified, and each picture carries a small dismiss control that
  clears it — revealing the original value unchanged — so a different image can be dropped in its place.
- **Cell geometry never changes.** The whole picture is scaled to fit and clipped inside the existing
  cell box (`object-fit: contain` with `overflow: hidden`); row heights and column widths are
  unaffected.
- **Raster only, 10 MiB per file.** PNG, JPEG, GIF, WebP and BMP are accepted; `image/svg+xml` is
  deliberately refused, because an SVG is an XML document that can carry scripts and this surface has no
  sanitizer. Non-image and oversized drops are refused without modifying any cell, and a brief
  on-screen notice says why.
- **Deliberately absent:** images in CSV or XLSX import and export, clipboard copy and paste, undo and
  redo, formula awareness, real-time collaboration or cross-client sync, Excel-style floating,
  resizable or movable pictures, fan-out across neighbouring cells (a multi-file drop uses only the
  first acceptable file), and cross-page or cross-tab image drags, which arrive as URL strings rather
  than files and are ignored.

Live in-browser assessment is currently blocked by pre-existing defects in this repository that predate
this experiment and lie outside its scope — the client does not build or boot as delivered — so the
behaviour above is covered instead by the jsdom component tests under
`frontend/src/features/cellImages/__tests__/`. See
[documentation/cell-image-drop-experiment.md](./documentation/cell-image-drop-experiment.md) for the
full write-up, including the ephemerality contract, the security posture, known limitations, and those
pre-existing defects.

## Technology Stack

- Frontend: React.js
- Backend: Node.js with Express.js
- Database: MongoDB
- State Management: Redux
- Styling: Styled-components
- Testing: Jest and React Testing Library

## Getting Started

### Prerequisites

- Node.js (v14 or later)
- npm (v6 or later)
- MongoDB (v4 or later)

### Installation

1. Clone the repository:
   ```
   git clone https://github.com/yourusername/excel-clone.git
   ```

2. Navigate to the project directory:
   ```
   cd excel-clone
   ```

3. Install dependencies:
   ```
   npm install
   ```

4. Set up environment variables:
   Create a `.env` file in the root directory and add the following:
   ```
   MONGODB_URI=your_mongodb_connection_string
   PORT=3000
   ```

5. Start the development server:
   ```
   npm run dev
   ```

## Usage

1. Open your web browser and navigate to `http://localhost:3000`
2. Create a new spreadsheet or open an existing one
3. Use the toolbar to format cells, enter formulas, or import/export data
4. Your work is automatically saved to the database

## API Documentation

For detailed API documentation, please refer to the [API.md](./API.md) file.

## Contributing

We welcome contributions to the Microsoft Excel Clone project. Please read our [CONTRIBUTING.md](./CONTRIBUTING.md) for details on our code of conduct and the process for submitting pull requests.

## License

This project is licensed under the MIT License - see the [LICENSE.md](./LICENSE.md) file for details.

## Acknowledgements

- [React.js](https://reactjs.org/)
- [Node.js](https://nodejs.org/)
- [Express.js](https://expressjs.com/)
- [MongoDB](https://www.mongodb.com/)
- [Redux](https://redux.js.org/)
- [Styled-components](https://styled-components.com/)