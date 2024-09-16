#!/bin/bash

# Check and install required system dependencies
echo "Checking and installing system dependencies..."
if ! command -v python3 &> /dev/null; then
    sudo apt-get update
    sudo apt-get install -y python3 python3-pip
fi

if ! command -v node &> /dev/null; then
    curl -sL https://deb.nodesource.com/setup_14.x | sudo -E bash -
    sudo apt-get install -y nodejs
fi

# Set up Python virtual environment
echo "Setting up Python virtual environment..."
python3 -m venv venv
source venv/bin/activate

# Install Python dependencies from requirements.txt
echo "Installing Python dependencies..."
pip install -r requirements.txt

# Install Node.js and npm
echo "Checking Node.js and npm..."
if ! command -v npm &> /dev/null; then
    sudo apt-get install -y npm
fi

# Install frontend dependencies
echo "Installing frontend dependencies..."
cd frontend
npm install
cd ..

# Set up Google Cloud SDK
echo "Setting up Google Cloud SDK..."
# HUMAN ASSISTANCE NEEDED
# Please provide the specific steps to install and configure Google Cloud SDK for this project

# Configure local environment variables
echo "Configuring local environment variables..."
cp .env.example .env
# HUMAN ASSISTANCE NEEDED
# Please update the .env file with the necessary environment variables

# Initialize local database
echo "Initializing local database..."
# HUMAN ASSISTANCE NEEDED
# Please provide the specific steps to initialize the local database for this project

# Run any necessary migrations
echo "Running database migrations..."
python manage.py migrate

# Print setup completion message and next steps
echo "Development environment setup complete!"
echo "Next steps:"
echo "1. Review and update the .env file with your local configuration"
echo "2. Start the backend server: python manage.py runserver"
echo "3. Start the frontend development server: cd frontend && npm start"