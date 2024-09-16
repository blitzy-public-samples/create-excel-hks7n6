# Microsoft Excel Clone

A web-based clone of Microsoft Excel with essential spreadsheet functionalities.

## Features

- Create, edit, and save spreadsheets
- Basic cell formatting (bold, italic, underline, cell color)
- Formula support for basic arithmetic operations
- Data import/export (CSV)
- Responsive design for desktop and mobile use

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