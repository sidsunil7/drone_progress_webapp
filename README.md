# Interactive Tracker Viewer Web App

A web-based interactive viewer for solar tracker layouts. Click on any tracker in the stage layout to view its individual image, stage, and status.

## Features

- **Interactive Layout**: Click on any tracker in the stage overlay image
- **Tracker Details**: View tracker ID, current stage, and status
- **Individual Tracker Images**: See the detailed TIFF image for each tracker
- **Multiple Layouts**: Switch between different date layouts if available

## Setup

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Run the Application

```bash
python app.py
```

The server will start on `http://localhost:5000`

### 3. Open in Browser

Navigate to: `http://localhost:5000`

## File Structure

```
tracker_webapp/
├── app.py              # Flask backend server
├── templates/
│   └── index.html      # Main web interface
├── requirements.txt    # Python dependencies
└── README.md          # This file
```

## How It Works

1. **Backend (app.py)**: 
   - Flask server that serves the web interface
   - API endpoints for:
     - Getting available layouts
     - Loading layout data (boundaries, tracker info)
     - Converting click coordinates to tracker IDs
     - Serving tracker TIFF images

2. **Frontend (index.html)**:
   - Displays the stage layout JPG image
   - Handles mouse clicks on the image
   - Converts click coordinates and queries the backend
   - Displays tracker details and images

## API Endpoints

- `GET /` - Main web interface
- `GET /api/layouts` - List available layouts
- `GET /api/layout/<layout_name>` - Get layout data (boundaries, tracker info)
- `GET /api/image/layout/<filename>` - Serve layout JPG image
- `GET /api/click?x=<x>&y=<y>&layout=<layout>` - Find tracker at click coordinates
- `GET /api/tracker/<tracker_id>` - Get individual tracker TIFF as base64 image

## Requirements

- Python 3.8+
- Flask
- rasterio
- numpy
- Pillow (PIL)

## Notes

- The app expects files in the parent directory:
  - `tracker_webapp/layout_data/` - Contains stage overlay JPGs, JSON, and CSV files
  - `Output_Lewis/` - Contains individual tracker TIFF files
  - `Lewistifs/` - Contains original TIFF files for coordinate conversion

- Large images are automatically resized for web display
- The app handles coordinate conversion between JPG pixels and TIFF geographic coordinates

