# Microsoft Excel Clone

A web-based clone of Microsoft Excel with essential spreadsheet functionalities.

## Features

- Create, edit, and save spreadsheets
- Basic cell formatting (bold, italic, underline, cell color)
- Formula support for basic arithmetic operations
- Data import/export (CSV)
- Responsive design for desktop and mobile use

### Experimental: Drag-and-Drop Images into Cells

An experimental prototype, not a supported feature: you can drag an image file from your file system and drop it onto a spreadsheet cell, and the image renders inside that cell's bounds. The full write-up is in [documentation/cell-image-drop-experiment.md](./documentation/cell-image-drop-experiment.md).

- Ingestion is the HTML5 drag-and-drop API and nothing else. There is no click-to-upload file picker.
- Nothing is uploaded to a server and nothing is persisted. Each image is held in browser memory only, as a `blob:` object URL: no HTTP request is issued, no backend endpoint exists for it, and nothing is written to any database, to Cloud Storage, to Firestore, to `localStorage`, `sessionStorage`, or IndexedDB, or into any saved workbook.
- Ephemerality is by design. Images survive moving between routes within the same page session, and are lost on a page refresh or a tab close, which is explicitly acceptable for this experiment.
- Cell data is untouched. The image renders as a layer over the cell's value area, so the cell's `value` and `formula` are never modified, and each placed image carries a small dismiss control that reveals the original value unchanged and frees the cell for a different image.
- Grid geometry is preserved. The image is scaled to fit and clipped inside the existing cell box, using `object-fit: contain` with `overflow: hidden`, so row heights and column widths never change.
- Accepted formats are raster only: `image/png`, `image/jpeg`, `image/gif`, `image/webp`, and `image/bmp`. `image/svg+xml` is deliberately not accepted, because an SVG is an XML document that can carry scripts and this surface has no sanitizer. A 10 MiB per-file ceiling applies, alongside the pixel and retention ceilings described in the note. Non-image and oversized drops are refused without modifying any cell, and surface a brief on-screen notice.
- Deliberately not supported: images in CSV or XLSX import and export, clipboard copy and paste of images, undo and redo for images, formula awareness of images, real-time collaboration or syncing of images, Excel-style floating, resizable, or movable pictures, and multi-cell fan-out. A multi-file drop uses only the first acceptable image, and cross-page or cross-tab image drags, which arrive as URL strings rather than files, are ignored.
- The goal is purely a visual assessment of the implications of putting pictures in spreadsheet cells: how much of an image a default cell can show, how it clips, aspect-ratio behaviour, the legibility of a value beneath an image, and perceived grid performance. It is an experiment for future projects.
- Live in-browser assessment is currently blocked by pre-existing defects in this repository that predate this experiment and are out of scope for it, because the client does not build or boot as delivered. The behaviour is covered instead by the jsdom component tests under `frontend/src/features/cellImages/__tests__/`.

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