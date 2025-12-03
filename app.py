from flask import Flask, render_template, send_file, jsonify, request
import os
import json
import csv
import rasterio
import numpy as np
from PIL import Image
from io import BytesIO
import base64

app = Flask(__name__)

# Configuration - paths relative to project root
# For Railway deployment, use paths relative to app directory
BASE_DIR = os.environ.get('BASE_DIR', os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LAYOUT_DIR = os.environ.get('LAYOUT_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), "layout_data"))
OUTPUT_DIR = os.environ.get('OUTPUT_DIR', os.path.join(BASE_DIR, "Output_Lewis"))
LEWISTIFS_DIR = os.environ.get('LEWISTIFS_DIR', os.path.join(BASE_DIR, "Lewistifs"))

def load_tracker_boundaries(json_path):
    """Load tracker boundaries from JSON"""
    with open(json_path, 'r') as f:
        data = json.load(f)
    boundaries = {}
    for table in data.get('tableDetails', []):
        tracker_id = table.get('tableName', '')
        tr_lat = table.get('TopRightLatitude')
        tr_lon = table.get('TopRightLongitude')
        bl_lat = table.get('BottomLeftLatitude')
        bl_lon = table.get('BottomLeftLongitude')
        if tracker_id and None not in [tr_lat, tr_lon, bl_lat, bl_lon]:
            boundaries[tracker_id] = {
                'min_lon': min(bl_lon, tr_lon),
                'max_lon': max(bl_lon, tr_lon),
                'min_lat': min(bl_lat, tr_lat),
                'max_lat': max(bl_lat, tr_lat)
            }
    return boundaries

def load_tracker_info(csv_path):
    """Load tracker stage and status from CSV"""
    tracker_info = {}
    if os.path.exists(csv_path):
        with open(csv_path, 'r', newline='') as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                tracker_id = row.get('Tracker ID', '').strip()
                if tracker_id:
                    tracker_info[tracker_id] = {
                        'stage': row.get('Current_stage', '').strip(),
                        'status': row.get('Status', '').strip()
                    }
    return tracker_info

def tif_to_base64(tif_path, max_size=2000):
    """Convert TIFF to base64 PNG for web display"""
    try:
        Image.MAX_IMAGE_PIXELS = 2_000_000_000  # Increase limit for large images
        
        with rasterio.open(tif_path) as src:
            img_data = src.read()
            
            # Handle multi-band images
            if len(img_data.shape) == 3:
                if img_data.shape[0] >= 3:
                    # RGB - transpose to (height, width, channels)
                    img_array = np.transpose(img_data[:3], (1, 2, 0))
                elif img_data.shape[0] == 1:
                    # Single band - convert to grayscale RGB
                    img_array = np.dstack([img_data[0], img_data[0], img_data[0]])
                else:
                    img_array = np.transpose(img_data, (1, 2, 0))
            else:
                # 2D grayscale
                img_array = np.dstack([img_data, img_data, img_data])
            
            # Normalize to 0-255 range
            if img_array.dtype != np.uint8:
                if len(img_array.shape) == 2:
                    img_min = np.nanmin(img_array)
                    img_max = np.nanmax(img_array)
                    if img_max > img_min:
                        img_array = ((img_array - img_min) / (img_max - img_min) * 255).astype(np.uint8)
                    else:
                        img_array = np.zeros_like(img_array, dtype=np.uint8)
                else:
                    img_array_normalized = np.zeros_like(img_array, dtype=np.uint8)
                    for i in range(img_array.shape[2]):
                        band = img_array[:, :, i]
                        band_min = np.nanmin(band)
                        band_max = np.nanmax(band)
                        if band_max > band_min:
                            img_array_normalized[:, :, i] = ((band - band_min) / (band_max - band_min) * 255).astype(np.uint8)
                    img_array = img_array_normalized
            
            img = Image.fromarray(img_array)
            
            # Resize if too large
            if max(img.width, img.height) > max_size:
                ratio = max_size / max(img.width, img.height)
                new_size = (int(img.width * ratio), int(img.height * ratio))
                img = img.resize(new_size, Image.LANCZOS)
            
            buffer = BytesIO()
            img.save(buffer, format='PNG')
            return base64.b64encode(buffer.getvalue()).decode('utf-8')
    except Exception as e:
        print(f"Error converting TIFF: {e}")
        import traceback
        traceback.print_exc()
        return None

@app.route('/')
def index():
    """Main page"""
    return render_template('index.html')

@app.route('/api/dates')
def get_dates():
    """Get list of available dates from subfolders"""
    if not os.path.exists(LAYOUT_DIR):
        return jsonify({'error': 'Layout directory not found'}), 404
    
    dates = []
    # Look for date folders (e.g., Lewis20251009, Lewis20251016)
    for item in os.listdir(LAYOUT_DIR):
        item_path = os.path.join(LAYOUT_DIR, item)
        if os.path.isdir(item_path) and item.startswith('Lewis'):
            # Extract date from folder name (e.g., Lewis20251009 -> 20251009)
            date_str = item.replace('Lewis', '')
            if date_str.isdigit() and len(date_str) == 8:  # YYYYMMDD format
                dates.append({
                    'date': date_str,
                    'folder': item,
                    'display': f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"  # YYYY-MM-DD format
                })
    
    # Sort dates chronologically
    dates.sort(key=lambda x: x['date'])
    return jsonify({'dates': dates})

@app.route('/api/layout/<date_str>')
def get_layout_data(date_str):
    """Get layout data (image, boundaries, tracker info) for a specific date"""
    # Find the date folder
    date_folder = f"Lewis{date_str}"
    date_folder_path = os.path.join(LAYOUT_DIR, date_folder)
    
    if not os.path.exists(date_folder_path):
        return jsonify({'error': f'Date folder not found: {date_folder}'}), 404
    
    # Find the base image (without overlay)
    base_image_files = [f for f in os.listdir(date_folder_path) 
                       if f.endswith('.jpg') and 
                       not f.endswith('_overlay.jpg') and 
                       not f.endswith('_stage_overlay.jpg') and
                       not f.endswith('_status_overlay.jpg') and
                       not f.endswith('_stage_status_overlay.jpg') and
                       not f.endswith('_web.jpg')]
    
    if not base_image_files:
        return jsonify({'error': f'Base image not found for date {date_str}'}), 404
    
    base_image_name = base_image_files[0]
    date_match = base_image_name.replace('.jpg', '')
    
    # Find overlay image for reference (optional)
    stage_status_overlay_files = [f for f in os.listdir(date_folder_path) if f.endswith('_stage_status_overlay.jpg')]
    stage_overlay_files = [f for f in os.listdir(date_folder_path) if f.endswith('_stage_overlay.jpg') and not f.endswith('_stage_status_overlay.jpg')]
    
    overlay_image_name = None
    if stage_status_overlay_files:
        overlay_image_name = stage_status_overlay_files[0]
    elif stage_overlay_files:
        overlay_image_name = stage_overlay_files[0]
    
    # Load JSON and CSV
    json_path = os.path.join(LAYOUT_DIR, "Lewis-NY_construction_AI_corrected_1.json")
    csv_path = os.path.join(date_folder_path, f"{date_match}_tracker_stages.csv")
    
    if not os.path.exists(json_path):
        return jsonify({'error': 'JSON file not found'}), 404
    
    boundaries = load_tracker_boundaries(json_path)
    tracker_info = load_tracker_info(csv_path) if os.path.exists(csv_path) else {}
    
    # Get TIFF transform info
    # Try to find TIFF in the date folder first, then fall back to LEWISTIFS_DIR
    tif_path = os.path.join(date_folder_path, f"{date_match}.tif")
    if not os.path.exists(tif_path):
        # Fall back to LEWISTIFS_DIR
        tif_path = os.path.join(LEWISTIFS_DIR, f"{date_match}.tif")
        if not os.path.exists(tif_path):
            return jsonify({'error': 'TIFF file not found'}), 404
    
    with rasterio.open(tif_path) as src:
        transform = list(src.transform)
        width = src.width
        height = src.height
    
    # Get base image dimensions
    base_image_path = os.path.join(date_folder_path, base_image_name)
    original_width = None
    original_height = None
    display_width = None
    display_height = None
    scale_factor = 1.0
    
    if os.path.exists(base_image_path):
        Image.MAX_IMAGE_PIXELS = 2_000_000_000
        with Image.open(base_image_path) as img:
            original_width = img.width
            original_height = img.height
            
            # Check if downscaled version exists
            downscaled_path = base_image_path.replace('.jpg', '_web.jpg')
            if os.path.exists(downscaled_path):
                with Image.open(downscaled_path) as downscaled_img:
                    display_width = downscaled_img.width
                    display_height = downscaled_img.height
                    # Calculate scale factor from original to display
                    scale_factor = original_width / display_width
            else:
                display_width = original_width
                display_height = original_height
    
    response_data = {
        'boundaries': boundaries,
        'tracker_info': tracker_info,
        'transform': transform,
        'tif_width': width,
        'tif_height': height,
        'base_image': f'/api/image/layout/{date_str}/{base_image_name}',
        'original_image_width': original_width,
        'original_image_height': original_height,
        'display_image_width': display_width,
        'display_image_height': display_height,
        'image_scale_factor': scale_factor,
        'date': date_str
    }
    
    # Add overlay image path if available (for reference)
    if overlay_image_name:
        response_data['overlay_image'] = f'/api/image/layout/{date_str}/{overlay_image_name}'
    
    return jsonify(response_data)

@app.route('/api/image/layout/<date_str>/<path:filename>')
def get_layout_image(date_str, filename):
    """Serve layout JPG image - creates downscaled version if too large"""
    # Find the date folder
    date_folder = f"Lewis{date_str}"
    date_folder_path = os.path.join(LAYOUT_DIR, date_folder)
    
    if not os.path.exists(date_folder_path):
        return jsonify({'error': f'Date folder not found: {date_folder}'}), 404
    
    file_path = os.path.join(date_folder_path, filename)
    if not os.path.exists(file_path):
        print(f"Image not found: {file_path}")
        return jsonify({'error': f'Image not found: {file_path}'}), 404
    
    try:
        file_size = os.path.getsize(file_path)
        print(f"Serving image: {filename} ({file_size} bytes)")
        
        # If image is larger than 50MB, create and serve a downscaled version
        MAX_SIZE_FOR_DIRECT_SERVE = 50 * 1024 * 1024  # 50MB
        
        if file_size > MAX_SIZE_FOR_DIRECT_SERVE:
            print(f"Image too large ({file_size} bytes), creating downscaled version...")
            # Create downscaled version
            downscaled_path = os.path.join(date_folder_path, filename.replace('.jpg', '_web.jpg'))
            
            # Check if downscaled version already exists
            if not os.path.exists(downscaled_path):
                Image.MAX_IMAGE_PIXELS = 2_000_000_000
                with Image.open(file_path) as img:
                    # Calculate new size (max 4000px on longest side)
                    max_dimension = 4000
                    if img.width > max_dimension or img.height > max_dimension:
                        ratio = min(max_dimension / img.width, max_dimension / img.height)
                        new_size = (int(img.width * ratio), int(img.height * ratio))
                        img_resized = img.resize(new_size, Image.LANCZOS)
                        img_resized.save(downscaled_path, 'JPEG', quality=85, optimize=True)
                        print(f"Created downscaled version: {downscaled_path} ({os.path.getsize(downscaled_path)} bytes)")
                    else:
                        # Image is small enough, just copy it
                        import shutil
                        shutil.copy2(file_path, downscaled_path)
            
            # Serve the downscaled version
            file_path = downscaled_path
            file_size = os.path.getsize(file_path)
            print(f"Serving downscaled version: {file_size} bytes")
        
        response = send_file(file_path, mimetype='image/jpeg')
        response.headers['Cache-Control'] = 'public, max-age=3600'
        response.headers['Content-Length'] = str(file_size)
        return response
    except Exception as e:
        print(f"Error serving image: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/tracker/<date_str>/<tracker_id>')
def get_tracker_image(date_str, tracker_id):
    """Get individual tracker TIFF as base64 for a specific date"""
    # Find tracker TIFF - try multiple locations
    tracker_tif = f"{tracker_id}_boundary.tif"
    
    # Find the date folder to get the date_match
    date_folder = f"Lewis{date_str}"
    date_folder_path = os.path.join(LAYOUT_DIR, date_folder)
    
    if not os.path.exists(date_folder_path):
        return jsonify({'error': f'Date folder not found: {date_folder}'}), 404
    
    # Find any image file to get date_match (e.g., LewisFull520251001)
    # Try multiple patterns to find the date_match prefix
    base_image_files = [f for f in os.listdir(date_folder_path) 
                       if f.endswith('.jpg') and 
                       not f.endswith('_web.jpg')]
    
    if not base_image_files:
        return jsonify({'error': f'No image files found for date {date_str}'}), 404
    
    # Extract date_match from the first image file found
    # Remove common suffixes to get base name
    first_image = base_image_files[0]
    date_match = first_image.replace('_stage_overlay.jpg', '').replace('_status_overlay.jpg', '').replace('_stage_status_overlay.jpg', '').replace('.jpg', '')
    
    # Try different possible paths - prioritize layout_data subfolder
    possible_paths = [
        # First priority: layout_data/{date_folder}/{date_match}/{tracker_id}_boundary.tif
        os.path.join(date_folder_path, date_match, tracker_tif),
        # Fallback: OUTPUT_DIR paths
        os.path.join(OUTPUT_DIR, date_match, tracker_tif),
        os.path.join(OUTPUT_DIR, date_match, tracker_id[:5], tracker_tif),  # A01T01 folder
        os.path.join(OUTPUT_DIR, date_match, tracker_id[:6], tracker_tif),  # A01T01R folder
    ]
    
    for path in possible_paths:
        if os.path.exists(path):
            base64_img = tif_to_base64(path)
            if base64_img:
                return jsonify({'image': f'data:image/png;base64,{base64_img}'})
    
    # Return error with some debug info
    return jsonify({
        'error': f'Tracker image not found: {tracker_id} for date {date_str}',
        'expected_path': os.path.join(date_folder_path, date_match, tracker_tif)
    }), 404

@app.route('/api/click')
def handle_click():
    """Handle click event - convert pixel to lat/lon and find tracker"""
    try:
        x = float(request.args.get('x'))
        y = float(request.args.get('y'))
        date_str = request.args.get('date')
        
        if not date_str:
            return jsonify({'error': 'Date required'}), 400
        
        # Find the date folder to get the date_match
        date_folder = f"Lewis{date_str}"
        date_folder_path = os.path.join(LAYOUT_DIR, date_folder)
        
        if not os.path.exists(date_folder_path):
            return jsonify({'error': f'Date folder not found: {date_folder}'}), 404
        
        # Find any image file to get date_match
        # Try multiple patterns to find the date_match prefix
        base_image_files = [f for f in os.listdir(date_folder_path) 
                           if f.endswith('.jpg') and 
                           not f.endswith('_web.jpg')]
        
        if not base_image_files:
            return jsonify({'error': f'No image files found for date {date_str}'}), 404
        
        # Extract date_match from the first image file found
        # Remove common suffixes to get base name
        first_image = base_image_files[0]
        date_match = first_image.replace('_stage_overlay.jpg', '').replace('_status_overlay.jpg', '').replace('_stage_status_overlay.jpg', '').replace('.jpg', '')
        
        # Try to find TIFF in the date folder first, then fall back to LEWISTIFS_DIR
        tif_path = os.path.join(date_folder_path, f"{date_match}.tif")
        if not os.path.exists(tif_path):
            # Fall back to LEWISTIFS_DIR
            tif_path = os.path.join(LEWISTIFS_DIR, f"{date_match}.tif")
            if not os.path.exists(tif_path):
                return jsonify({'error': 'TIFF file not found'}), 404
        
        with rasterio.open(tif_path) as src:
            # Convert pixel coordinates to geographic coordinates
            # Note: rasterio uses (row, col) = (y, x)
            lon, lat = src.xy(y, x)
        
        # Load boundaries
        json_path = os.path.join(LAYOUT_DIR, "Lewis-NY_construction_AI_corrected_1.json")
        if not os.path.exists(json_path):
            return jsonify({'error': 'JSON file not found'}), 404
        
        boundaries = load_tracker_boundaries(json_path)
        
        # Find tracker
        for tracker_id, bounds in boundaries.items():
            if (bounds['min_lon'] <= lon <= bounds['max_lon'] and
                bounds['min_lat'] <= lat <= bounds['max_lat']):
                return jsonify({'tracker_id': tracker_id})
        
        return jsonify({'tracker_id': None})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug_mode = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    
    print("="*60)
    print("Starting Tracker Web App")
    print("="*60)
    print(f"Layout Directory: {LAYOUT_DIR}")
    print(f"Output Directory: {OUTPUT_DIR}")
    print(f"Lewistifs Directory: {LEWISTIFS_DIR}")
    print(f"Port: {port}")
    print(f"Debug Mode: {debug_mode}")
    print("="*60)
    
    if not debug_mode:
        print("\nRunning in production mode")
        print("="*60)
    else:
        print("\nOpen your browser and navigate to: http://localhost:5000")
        print("="*60)
    
    app.run(debug=debug_mode, port=port, host='0.0.0.0')

