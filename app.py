from flask import Flask, render_template, send_file, jsonify, request, make_response
import os
import json
import csv
import rasterio
from rasterio.warp import transform as rio_transform
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO
import base64
import re

app = Flask(__name__)

# Configuration - paths relative to project root
# For Railway deployment, use paths relative to app directory
BASE_DIR = os.environ.get('BASE_DIR', os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASE_LAYOUT_DIR = os.environ.get('LAYOUT_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), "layout_data"))
OUTPUT_DIR = os.environ.get('OUTPUT_DIR', os.path.join(BASE_DIR, "Output_Lewis"))
LEWISTIFS_DIR = os.environ.get('LEWISTIFS_DIR', os.path.join(BASE_DIR, "Lewistifs"))
SONRISA_JSON_PATH = os.environ.get(
    'SONRISA_JSON_PATH',
    os.path.join(BASE_LAYOUT_DIR, "Sonrisa", "Sonrisa_construction_AI.json")
)

ZONE_CODE_PATTERN = re.compile(r"^([A-Za-z])(\d{1,2})")
TIME_TOKEN_PATTERN = re.compile(r"^(\d{1,2})(am|pm)$", re.IGNORECASE)
STAGE_COLORS = {
    "pile": (128, 0, 128, 200),
    "torque_tube": (200, 150, 255, 200),
    "module_rails": (0, 0, 139, 200),
    "solar_panel": (135, 206, 250, 200),
}


def normalize_status(status):
    """Normalize status strings to snake_case like 'not_started', 'in_progress', 'completed'."""
    if not status:
        return ""
    return status.strip().lower().replace(" ", "_")


def compute_current_stage_from_row(row):
    """
    Given a CSV row with per-stage columns like:
    - pile_stage, torque_tube_stage, module_rails_stage, solar_panel_stage

    Compute:
    - current_stage (one of: pile, torque_tube, module_rails, solar_panel)
    - current_status (normalized)

    Priority order:
    pile < torque_tube < module_rails < solar_panel

    Logic:
    - If any stage is 'in_progress', pick the highest-priority stage that is in_progress.
    - Else, if any stage is 'completed', pick the highest-priority completed stage.
    - Else, fall back to the first stage that has any status; if none, use pile/not_started.
    """
    stage_order = ["pile", "torque_tube", "module_rails", "solar_panel"]
    col_map = {
        "pile": "pile_stage",
        "torque_tube": "torque_tube_stage",
        "module_rails": "module_rails_stage",
        "solar_panel": "solar_panel_stage",
    }

    stage_statuses = {}
    for stage, col in col_map.items():
        raw = row.get(col, "") if row is not None else ""
        norm = normalize_status(raw)
        stage_statuses[stage] = norm or None

    # 1) Prefer any stage in progress (latest in the pipeline)
    in_progress_stages = [s for s in stage_order if stage_statuses.get(s) == "in_progress"]
    if in_progress_stages:
        current_stage = in_progress_stages[-1]
        return current_stage, "in_progress"

    # 2) Otherwise, use the latest completed stage
    completed_stages = [s for s in stage_order if stage_statuses.get(s) == "completed"]
    if completed_stages:
        current_stage = completed_stages[-1]
        return current_stage, "completed"

    # 3) Fallback: first stage with any status, else pile/not_started
    for s in stage_order:
        if stage_statuses.get(s):
            return s, stage_statuses[s]

    return "pile", "not_started"


def compute_current_stage_from_installation_row(row):
    """
    Given a CSV row with per-stage installation columns like:
    - pile_installation, lower_journal_installation, ..., solar_module_installation
    Compute current stage + status using the same priority logic.
    """
    stage_order = [
        "pile_installation",
        "torque_tube_installation",
        "module_rail_installation",
        "solar_module_installation",
    ]
    stage_label_map = {
        "pile_installation": "pile",
        "torque_tube_installation": "torque_tube",
        "module_rail_installation": "module_rails",
        "solar_module_installation": "solar_panel",
    }

    stage_statuses = {}
    for stage in stage_order:
        raw = row.get(stage, "") if row is not None else ""
        stage_statuses[stage] = normalize_status(raw) or None

    in_progress_stages = [s for s in stage_order if stage_statuses.get(s) == "in_progress"]
    if in_progress_stages:
        current_stage = in_progress_stages[-1]
        return stage_label_map[current_stage], "in_progress"

    completed_stages = [s for s in stage_order if stage_statuses.get(s) == "completed"]
    if completed_stages:
        last_completed = completed_stages[-1]
        last_idx = stage_order.index(last_completed)
        if last_idx < len(stage_order) - 1:
            next_stage = stage_order[last_idx + 1]
            next_status = stage_statuses.get(next_stage) or "not_started"
            if next_status == "not_started":
                return stage_label_map[last_completed], "completed"
            return stage_label_map[next_stage], next_status
        return stage_label_map[last_completed], "completed"

    for s in stage_order:
        if stage_statuses.get(s):
            return stage_label_map[s], stage_statuses[s]

    return "pile", "not_started"


def normalize_zone_code(value):
    if not value:
        return None
    match = ZONE_CODE_PATTERN.match(value.strip())
    if not match:
        return None
    letter, number = match.group(1).upper(), match.group(2).zfill(2)
    if letter == "A":
        letter = "G"
    return f"{letter}{number}"


def extract_zone_code_from_name(value):
    if not value:
        return None
    match = ZONE_CODE_PATTERN.match(value.strip())
    if not match:
        return None
    letter, number = match.group(1), match.group(2)
    return normalize_zone_code(f"{letter}{number}")


def normalize_tracker_id(tracker_id):
    if not tracker_id:
        return None
    t = tracker_id.strip()
    if t.lower().endswith("_boundary_spine"):
        t = t[:-15]
    if t.lower().endswith("_boundary"):
        t = t[:-9]
    return t


def get_zone_aliases(zone):
    zone = normalize_zone_code(zone)
    if not zone:
        return []
    letter = zone[0].upper()
    number = zone[1:]
    try:
        compact_num = str(int(number))
    except ValueError:
        compact_num = number
    aliases = {f"{letter}{number}", f"{letter}{compact_num}"}
    if letter == "G":
        aliases.add(f"A{number}")
        aliases.add(f"A{compact_num}")
    return sorted(aliases)


def get_zone_folder_aliases(zone):
    zone = normalize_zone_code(zone)
    if not zone:
        return []
    letter = zone[0].upper()
    number = zone[1:]
    try:
        compact_num = str(int(number))
    except ValueError:
        compact_num = number
    return [f"{letter}{number}", f"{letter}{compact_num}"]


def parse_sonrisa_folder_info(folder_name):
    if not folder_name:
        return [], None, None
    tokens = re.split(r"[_-]+", folder_name)
    zones = []
    current_letter = None
    date_str = None
    time_token = None
    for token in tokens:
        if date_str:
            time_match = TIME_TOKEN_PATTERN.match(token)
            if time_match:
                time_token = token.lower()
            continue
        if token.isdigit() and len(token) == 8:
            date_str = token
            continue
        match = re.match(r"^([A-Za-z])(\d{1,2})$", token)
        if match:
            letter = match.group(1).upper()
            number = match.group(2).zfill(2)
            if letter == "A":
                letter = "G"
            current_letter = letter
            zones.append(f"{letter}{number}")
            continue
        if token.isdigit() and len(token) <= 2:
            number = token.zfill(2)
            letter = current_letter or "G"
            zones.append(f"{letter}{number}")
            continue
    # Preserve order but unique
    seen = set()
    ordered = []
    for z in zones:
        if z not in seen:
            seen.add(z)
            ordered.append(z)
    return ordered, date_str, time_token


def parse_time_token_minutes(value):
    if not value:
        return None
    match = TIME_TOKEN_PATTERN.match(value.strip())
    if not match:
        return None
    hour = int(match.group(1))
    suffix = match.group(2).lower()
    hour = max(1, min(12, hour))
    if suffix == "am":
        return 0 if hour == 12 else hour * 60
    return 12 * 60 if hour == 12 else (hour + 12) * 60


def split_sonrisa_date_time(date_str):
    if not date_str:
        return None, None
    parts = date_str.split("_", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return date_str, None


def get_sonrisa_all_dates(project_layout_dir):
    if not project_layout_dir or not os.path.exists(project_layout_dir):
        return []
    dates = {}
    for item in os.listdir(project_layout_dir):
        item_path = os.path.join(project_layout_dir, item)
        if not os.path.isdir(item_path):
            continue
        _, date_str, _ = parse_sonrisa_folder_info(item.strip())
        if not date_str or not date_str.isdigit() or len(date_str) != 8:
            continue
        display = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
        dates[date_str] = {
            "date": date_str,
            "folder": item,
            "display": display,
        }
    ordered = sorted(dates.values(), key=lambda x: x["date"])
    return ordered


def get_available_projects():
    if not os.path.exists(BASE_LAYOUT_DIR):
        return []
    return sorted([
        name for name in os.listdir(BASE_LAYOUT_DIR)
        if os.path.isdir(os.path.join(BASE_LAYOUT_DIR, name))
    ])


def resolve_project_name(raw_project):
    if not raw_project:
        return None
    available = get_available_projects()
    if not available:
        return None
    lookup = {name.lower(): name for name in available}
    return lookup.get(raw_project.strip().lower())


def get_project_from_request():
    raw_project = (
        request.view_args.get("project")
        if request.view_args
        else None
    )
    if not raw_project:
        raw_project = request.args.get("project") or request.cookies.get("project")
    return resolve_project_name(raw_project) or "Lewis"


def get_layout_dir(project):
    return os.path.join(BASE_LAYOUT_DIR, project)


def get_sonrisa_json_path(project_layout_dir=None):
    candidates = []
    if SONRISA_JSON_PATH:
        candidates.append(SONRISA_JSON_PATH)
    if project_layout_dir:
        candidates.append(os.path.join(project_layout_dir, "Sonrisa_construction_AI.json"))
    # Fallback to layout_data/Sonrisa if project_layout_dir is not provided
    candidates.append(os.path.join(BASE_LAYOUT_DIR, "Sonrisa", "Sonrisa_construction_AI.json"))
    candidates.append(os.path.join(BASE_DIR, "Sonrisa_construction_AI.json"))
    for path in candidates:
        if path and os.path.exists(path):
            return path
    # Fallback: search for any Sonrisa JSON in BASE_DIR
    for root, _, files in os.walk(BASE_DIR):
        for fname in files:
            if fname.lower().startswith("sonrisa") and fname.lower().endswith(".json"):
                return os.path.join(root, fname)
    return None


def get_sonrisa_zone_bounds(json_path):
    if not json_path or not os.path.exists(json_path):
        return {}
    with open(json_path, 'r') as f:
        data = json.load(f)
    zone_bounds = {}
    for entry in data.get("tableDetails", []):
        zone = extract_zone_code_from_name(entry.get("tableName"))
        if not zone:
            continue
        coords = [
            entry.get("TopRightLatitude"),
            entry.get("TopRightLongitude"),
            entry.get("BottomLeftLatitude"),
            entry.get("BottomLeftLongitude"),
        ]
        if any(v is None for v in coords):
            continue
        top_lat, right_lon, bottom_lat, left_lon = coords
        min_lat = min(top_lat, bottom_lat)
        max_lat = max(top_lat, bottom_lat)
        min_lon = min(left_lon, right_lon)
        max_lon = max(left_lon, right_lon)
        if zone not in zone_bounds:
            zone_bounds[zone] = [min_lat, min_lon, max_lat, max_lon]
        else:
            zb = zone_bounds[zone]
            zb[0] = min(zb[0], min_lat)
            zb[1] = min(zb[1], min_lon)
            zb[2] = max(zb[2], max_lat)
            zb[3] = max(zb[3], max_lon)
    return zone_bounds


def get_sonrisa_available_zones(project_layout_dir):
    zones = set()
    if not project_layout_dir or not os.path.exists(project_layout_dir):
        return []
    for item in os.listdir(project_layout_dir):
        item_path = os.path.join(project_layout_dir, item)
        if not os.path.isdir(item_path):
            continue
        folder_zones, _, _ = parse_sonrisa_folder_info(item)
        for zone in folder_zones:
            zones.add(zone)
    return sorted(zones)


def find_sonrisa_zone_csv(date_folder_path, zone):
    if not date_folder_path or not zone:
        return None
    csv_path = None
    trackers_root = os.path.join(date_folder_path, "trackers")
    zone_folder_aliases = get_zone_folder_aliases(zone)
    if os.path.isdir(trackers_root):
        csv_names = ["tracker_status_v03.csv", "tracker_status_v02.csv", "tracker_status.csv"]
        for alias in zone_folder_aliases:
            for name in csv_names:
                candidate = os.path.join(trackers_root, alias, name)
                if os.path.exists(candidate):
                    csv_path = candidate
                    break
            if csv_path:
                break
        if not csv_path:
            for entry in os.listdir(trackers_root):
                for name in csv_names:
                    candidate = os.path.join(trackers_root, entry, name)
                    if os.path.exists(candidate):
                        csv_path = candidate
                        break
                if csv_path:
                    break

    if not csv_path and os.path.isdir(date_folder_path):
        csv_candidates = [f for f in os.listdir(date_folder_path) if f.lower().endswith('.csv')]
        for alias in get_zone_aliases(zone):
            for f in csv_candidates:
                if f.lower().startswith(alias.lower()):
                    csv_path = os.path.join(date_folder_path, f)
                    break
            if csv_path:
                break
        if not csv_path and csv_candidates:
            csv_path = os.path.join(date_folder_path, csv_candidates[0])

    return csv_path


def get_sonrisa_zone_stage(project_layout_dir, zone, date_id=None):
    date_folders = []
    if date_id:
        for item in os.listdir(project_layout_dir):
            folder_zones, folder_date, _ = parse_sonrisa_folder_info(item.strip())
            if zone in folder_zones and folder_date == date_id:
                date_folders.append(os.path.join(project_layout_dir, item))
    else:
        dates = get_sonrisa_zone_dates(project_layout_dir, zone)
        if not dates:
            return None
        latest = dates[-1]
        date_folder_path = find_sonrisa_date_folder(project_layout_dir, zone, latest["date"])
        if date_folder_path:
            date_folders.append(date_folder_path)

    if not date_folders:
        return None

    counts = {}
    for folder in date_folders:
        csv_path = find_sonrisa_zone_csv(folder, zone)
        if not csv_path or not os.path.exists(csv_path):
            continue
        tracker_info = load_tracker_info(csv_path)
        for info in tracker_info.values():
            stage = (info.get("stage") or "").lower().replace(" ", "_")
            if not stage:
                continue
            counts[stage] = counts.get(stage, 0) + 1

    if not counts:
        return None
    return max(counts.items(), key=lambda item: item[1])[0]


def get_sonrisa_zone_dates(project_layout_dir, zone):
    zone = normalize_zone_code(zone)
    if not zone or not project_layout_dir or not os.path.exists(project_layout_dir):
        return []
    dates = []
    for item in os.listdir(project_layout_dir):
        item_path = os.path.join(project_layout_dir, item)
        if not os.path.isdir(item_path):
            continue
        folder_zones, date_str, time_token = parse_sonrisa_folder_info(item.strip())
        if zone in folder_zones and date_str and date_str.isdigit() and len(date_str) == 8:
            date_id = f"{date_str}_{time_token}" if time_token else date_str
            display = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
            if time_token:
                display = f"{display} {time_token}"
            dates.append({
                'date': date_id,
                'folder': item,
                'display': display
            })
    dates.sort(
        key=lambda x: (
            split_sonrisa_date_time(x["date"])[0],
            parse_time_token_minutes(split_sonrisa_date_time(x["date"])[1]) or 0,
        )
    )
    return dates


def find_sonrisa_date_folder(project_layout_dir, zone, date_str):
    zone = normalize_zone_code(zone)
    if not zone or not project_layout_dir or not os.path.exists(project_layout_dir):
        return None
    target_date, target_time = split_sonrisa_date_time(date_str)
    for item in os.listdir(project_layout_dir):
        folder_zones, folder_date, folder_time = parse_sonrisa_folder_info(item.strip())
        if zone in folder_zones and folder_date == target_date:
            if target_time and folder_time and target_time != folder_time:
                continue
            return os.path.join(project_layout_dir, item)
    return None


def get_display_dimensions(width, height, max_size=2000):
    if max(width, height) <= max_size:
        return width, height, 1.0
    ratio = max_size / max(width, height)
    return int(width * ratio), int(height * ratio), ratio


def get_or_create_sonrisa_web_jpg(date_folder_path, zone_tif, max_dimension=4000):
    base_name = os.path.splitext(zone_tif)[0]
    web_jpg = f"{base_name}_web.jpg"
    web_path = os.path.join(date_folder_path, web_jpg)
    if os.path.exists(web_path):
        return web_path

    tif_path = os.path.join(date_folder_path, zone_tif)
    try:
        Image.MAX_IMAGE_PIXELS = 2_000_000_000
        with rasterio.open(tif_path) as src:
            img_data = src.read()

        if len(img_data.shape) == 3:
            if img_data.shape[0] >= 3:
                img_array = np.transpose(img_data[:3], (1, 2, 0))
            elif img_data.shape[0] == 1:
                img_array = np.dstack([img_data[0], img_data[0], img_data[0]])
            else:
                img_array = np.transpose(img_data, (1, 2, 0))
        else:
            img_array = np.dstack([img_data, img_data, img_data])

        if img_array.dtype != np.uint8:
            img_array_normalized = np.zeros_like(img_array, dtype=np.uint8)
            for i in range(img_array.shape[2]):
                band = img_array[:, :, i]
                band_min = np.nanmin(band)
                band_max = np.nanmax(band)
                if band_max > band_min:
                    img_array_normalized[:, :, i] = ((band - band_min) / (band_max - band_min) * 255).astype(np.uint8)
            img_array = img_array_normalized

        img = Image.fromarray(img_array)
        if max(img.width, img.height) > max_dimension:
            ratio = max_dimension / max(img.width, img.height)
            new_size = (int(img.width * ratio), int(img.height * ratio))
            img = img.resize(new_size, Image.LANCZOS)

        img.save(web_path, 'JPEG', quality=85, optimize=True)
        return web_path
    except Exception as e:
        print(f"Error creating Sonrisa web JPG: {e}")
        return None


def compute_zone_overall_bounds(zone_bounds):
    if not zone_bounds:
        return None
    min_lat = min(v[0] for v in zone_bounds.values())
    min_lon = min(v[1] for v in zone_bounds.values())
    max_lat = max(v[2] for v in zone_bounds.values())
    max_lon = max(v[3] for v in zone_bounds.values())
    lat_pad = (max_lat - min_lat) * 0.02
    lon_pad = (max_lon - min_lon) * 0.02
    return {
        'min_lat': min_lat - lat_pad,
        'min_lon': min_lon - lon_pad,
        'max_lat': max_lat + lat_pad,
        'max_lon': max_lon + lon_pad
    }


def build_sonrisa_block_map(zone_bounds, available_zones, zone_colors=None, image_size=(1600, 900), background_path=None):
    overall = compute_zone_overall_bounds(zone_bounds)
    if not zone_bounds or not overall:
        return None, None
    min_lat = overall['min_lat']
    min_lon = overall['min_lon']
    max_lat = overall['max_lat']
    max_lon = overall['max_lon']

    if background_path and os.path.exists(background_path):
        base_img = Image.open(background_path).convert("RGBA")
        width, height = base_img.size
        img = base_img.copy()
    else:
        width, height = image_size
        img = Image.new("RGBA", (width, height), (245, 245, 245, 255))

    draw = ImageDraw.Draw(img, "RGBA")
    available_set = set(available_zones or [])
    zone_colors = zone_colors or {}

    try:
        font = ImageFont.load_default()
    except Exception:
        font = None

    lon_range = max_lon - min_lon
    lat_range = max_lat - min_lat

    for zone, bounds in sorted(zone_bounds.items()):
        z_min_lat, z_min_lon, z_max_lat, z_max_lon = bounds
        x1 = int(((z_min_lon - min_lon) / lon_range) * width)
        x2 = int(((z_max_lon - min_lon) / lon_range) * width)
        y1 = int(((max_lat - z_max_lat) / lat_range) * height)
        y2 = int(((max_lat - z_min_lat) / lat_range) * height)

        if zone in zone_colors:
            fill = zone_colors[zone]
        elif zone in available_set:
            fill = (120, 220, 120, 200)
        else:
            fill = (255, 255, 255, 255)
        draw.rectangle([x1, y1, x2, y2], outline=(0, 0, 0, 200), width=2, fill=fill)

        cx = (x1 + x2) / 2
        cy = (y1 + y2) / 2
        label = zone
        if hasattr(draw, "textbbox"):
            bbox = draw.textbbox((0, 0), label, font=font)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]
        else:
            text_w, text_h = draw.textsize(label, font=font)
        draw.rectangle(
            [cx - text_w / 2 - 4, cy - text_h / 2 - 2, cx + text_w / 2 + 4, cy + text_h / 2 + 2],
            fill=(255, 255, 255, 220),
            outline=(0, 0, 0, 200),
            width=1
        )
        draw.text((cx - text_w / 2, cy - text_h / 2), label, fill=(0, 0, 0, 255), font=font)

    return img, overall


def find_project_json(project_layout_dir, project_name):
    preferred = os.path.join(project_layout_dir, f"{project_name}-NY_construction_AI_corrected_1.json")
    if os.path.exists(preferred):
        return preferred
    json_files = [f for f in os.listdir(project_layout_dir) if f.lower().endswith(".json")]
    if not json_files:
        return None
    return os.path.join(project_layout_dir, sorted(json_files)[0])


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


def reproject_boundary(boundary, src_crs, dst_crs):
    if not boundary or not src_crs or not dst_crs:
        return boundary
    if str(src_crs) == str(dst_crs):
        return boundary
    lons = [boundary["min_lon"], boundary["max_lon"], boundary["max_lon"], boundary["min_lon"]]
    lats = [boundary["min_lat"], boundary["min_lat"], boundary["max_lat"], boundary["max_lat"]]
    xs, ys = rio_transform(src_crs, dst_crs, lons, lats)
    return {
        "min_lon": min(xs),
        "max_lon": max(xs),
        "min_lat": min(ys),
        "max_lat": max(ys),
    }

def load_tracker_info(csv_path):
    """Load tracker stage and status from CSV.

    Supports three formats:
    1) Old format with 'Current_stage' and 'Status' columns (tracker_webapp layout_data).
    2) New per-stage format with columns:
       'pile_stage', 'torque_tube_stage', 'module_rails_stage', 'solar_panel_stage'.
    3) Installation format with columns:
       'pile_installation', 'lower_journal_installation', ..., 'solar_module_installation'.
    """
    tracker_info = {}
    if not csv_path or not os.path.exists(csv_path):
        return tracker_info

    with open(csv_path, 'r', newline='') as csvfile:
        reader = csv.DictReader(csvfile)
        fieldnames = reader.fieldnames or []

        has_current_cols = "Current_stage" in fieldnames and "Status" in fieldnames
        has_per_stage_cols = (
            "pile_stage" in fieldnames
            and "torque_tube_stage" in fieldnames
            and "module_rails_stage" in fieldnames
            and "solar_panel_stage" in fieldnames
        )
        has_installation_cols = (
            "pile_installation" in fieldnames
            and "torque_tube_installation" in fieldnames
            and "module_rail_installation" in fieldnames
            and "solar_module_installation" in fieldnames
        )

        for row in reader:
            tracker_id = (
                row.get('Tracker ID')
                or row.get('tracker_id')
                or row.get('trackerID')
                or ''
            ).strip()
            if not tracker_id:
                continue

            if has_current_cols:
                stage = (row.get('Current_stage') or '').strip()
                status = (row.get('Status') or '').strip()
                tracker_info[tracker_id] = {
                    'stage': stage,
                    'status': status,
                }
            elif has_per_stage_cols:
                stage, status = compute_current_stage_from_row(row)
                tracker_info[tracker_id] = {
                    'stage': stage,
                    'status': status,
                }
            elif has_installation_cols:
                stage, status = compute_current_stage_from_installation_row(row)
                tracker_info[tracker_id] = {
                    'stage': stage,
                    'status': status,
                }
            else:
                # Unknown CSV format; skip gracefully
                continue

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
def select_project():
    """Project selection page"""
    projects = get_available_projects()
    return render_template('select_project.html', projects=projects)


@app.route('/project/<project>')
def project_home(project):
    """Set project selection and render the main app"""
    resolved_project = resolve_project_name(project)
    if not resolved_project:
        return render_template('select_project.html', projects=get_available_projects()), 404
    response = make_response(render_template('index.html', project=resolved_project))
    response.set_cookie('project', resolved_project, samesite='Lax')
    return response

@app.route('/api/dates')
def get_dates():
    """Get list of available dates from subfolders"""
    project = get_project_from_request()
    project_layout_dir = get_layout_dir(project)
    if not os.path.exists(project_layout_dir):
        return jsonify({'error': 'Layout directory not found'}), 404

    if project == "Sonrisa":
        zone = normalize_zone_code(request.args.get('zone'))
        if not zone:
            return jsonify({'error': 'Zone required for Sonrisa'}), 400
        dates = get_sonrisa_zone_dates(project_layout_dir, zone)
        return jsonify({'dates': dates})

    dates = []
    # Look for date folders (e.g., Lewis20251009, Lewis20251016)
    for item in os.listdir(project_layout_dir):
        item_path = os.path.join(project_layout_dir, item)
        if os.path.isdir(item_path) and item.startswith(project):
            # Extract date from folder name (e.g., Lewis20251009 -> 20251009)
            date_str = item.replace(project, '')
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
    project = get_project_from_request()
    project_layout_dir = get_layout_dir(project)
    if project == "Sonrisa":
        zone = normalize_zone_code(request.args.get('zone'))
        if not zone:
            return jsonify({'error': 'Zone required for Sonrisa'}), 400
        date_folder_path = find_sonrisa_date_folder(project_layout_dir, zone, date_str)
        if not date_folder_path:
            return jsonify({'error': f'Date folder not found for {zone} {date_str}'}), 404
    else:
        date_folder = f"{project}{date_str}"
        date_folder_path = os.path.join(project_layout_dir, date_folder)
    
    if not os.path.exists(date_folder_path):
        if project == "Sonrisa":
            return jsonify({'error': f'Date folder not found for Sonrisa {date_str}'}), 404
        return jsonify({'error': f'Date folder not found: {date_folder}'}), 404

    if project == "Sonrisa":
        # Find zone TIFF
        tif_candidates = [f for f in os.listdir(date_folder_path) if f.lower().endswith('.tif')]
        zone_tif = None
        zone_aliases = get_zone_aliases(zone)
        for alias in zone_aliases:
            target = f"{alias.lower()}_zone.tif"
            for f in tif_candidates:
                if f.lower() == target:
                    zone_tif = f
                    break
            if zone_tif:
                break
        if not zone_tif:
            for f in tif_candidates:
                if f.lower().endswith('_zone.tif'):
                    zone_tif = f
                    break
        if not zone_tif and tif_candidates:
            zone_tif = tif_candidates[0]
        if not zone_tif:
            return jsonify({'error': 'Zone TIFF not found'}), 404
        tif_path = os.path.join(date_folder_path, zone_tif)

        # Load JSON and CSV
        json_path = get_sonrisa_json_path(project_layout_dir)
        csv_path = find_sonrisa_zone_csv(date_folder_path, zone)

        if not json_path or not os.path.exists(json_path):
            return jsonify({'error': 'JSON file not found'}), 404

        with rasterio.open(tif_path) as src:
            transform = list(src.transform)
            width = src.width
            height = src.height
            tif_crs = src.crs

        boundaries_raw = load_tracker_boundaries(json_path)
        boundaries = {}
        for tracker_id, b in boundaries_raw.items():
            if normalize_zone_code(tracker_id) != zone:
                continue
            normalized_id = normalize_tracker_id(tracker_id)
            if normalized_id:
                reproj = reproject_boundary(b, "EPSG:4326", tif_crs)
                boundaries[normalized_id] = reproj

        tracker_info_raw = load_tracker_info(csv_path) if csv_path and os.path.exists(csv_path) else {}
        tracker_info = {}
        for tracker_id, info in tracker_info_raw.items():
            normalized_id = normalize_tracker_id(tracker_id)
            if normalized_id:
                tracker_info[normalized_id] = info

        web_path = get_or_create_sonrisa_web_jpg(date_folder_path, zone_tif, max_dimension=4000)
        display_width = None
        display_height = None
        if web_path and os.path.exists(web_path):
            with Image.open(web_path) as img:
                display_width = img.width
                display_height = img.height
        if not display_width or not display_height:
            display_width, display_height, _ = get_display_dimensions(width, height, max_size=2000)

        response_data = {
            'boundaries': boundaries,
            'tracker_info': tracker_info,
            'transform': transform,
            'tif_width': width,
            'tif_height': height,
            'base_image': f'/api/sonrisa/image/{zone}/{date_str}',
            'original_image_width': width,
            'original_image_height': height,
            'display_image_width': display_width,
            'display_image_height': display_height,
            'image_scale_factor': (width / display_width) if display_width else 1.0,
            'date': date_str
        }
        return jsonify(response_data)
    
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
    json_path = find_project_json(project_layout_dir, project)
    csv_path = os.path.join(date_folder_path, f"{date_match}_tracker_stages.csv")
    
    if not json_path or not os.path.exists(json_path):
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
    project = get_project_from_request()
    if project == "Sonrisa":
        return jsonify({'error': 'Use Sonrisa image endpoint'}), 400
    project_layout_dir = get_layout_dir(project)
    date_folder = f"{project}{date_str}"
    date_folder_path = os.path.join(project_layout_dir, date_folder)
    
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


@app.route('/api/sonrisa/zones')
def get_sonrisa_zones():
    project = get_project_from_request()
    if project != "Sonrisa":
        return jsonify({'error': 'Invalid project'}), 400
    project_layout_dir = get_layout_dir(project)
    date_id = request.args.get("date")
    json_path = get_sonrisa_json_path(project_layout_dir)
    zone_bounds = get_sonrisa_zone_bounds(json_path)
    available_zones = get_sonrisa_available_zones(project_layout_dir)
    zone_stage = {
        zone: get_sonrisa_zone_stage(project_layout_dir, zone, date_id=date_id)
        for zone in available_zones
    }
    if date_id:
        available_zones = [zone for zone in available_zones if zone_stage.get(zone)]

    if not zone_bounds:
        return jsonify({'error': 'Sonrisa JSON boundaries not found'}), 404

    overall_bounds = compute_zone_overall_bounds(zone_bounds)
    if not overall_bounds:
        return jsonify({'error': 'Unable to compute zone bounds'}), 500

    return jsonify({
        'zones': [
            {
                'zone': zone,
                'bounds': {
                    'min_lat': bounds[0],
                    'min_lon': bounds[1],
                    'max_lat': bounds[2],
                    'max_lon': bounds[3]
                },
                'available': zone in available_zones,
                'stage': zone_stage.get(zone)
            }
            for zone, bounds in sorted(zone_bounds.items())
        ],
        'available_zones': available_zones,
        'overall_bounds': overall_bounds,
        'block_map_url': '/api/sonrisa/block_map'
    })


@app.route('/api/sonrisa/block_map')
def get_sonrisa_block_map():
    project = get_project_from_request()
    if project != "Sonrisa":
        return jsonify({'error': 'Invalid project'}), 400
    project_layout_dir = get_layout_dir(project)
    date_id = request.args.get("date")
    existing_map = os.path.join(project_layout_dir, "sonrisa_block_map.jpg")
    json_path = get_sonrisa_json_path(project_layout_dir)
    zone_bounds = get_sonrisa_zone_bounds(json_path)
    available_zones = get_sonrisa_available_zones(project_layout_dir)
    zone_colors = {}
    for zone in available_zones:
        stage = get_sonrisa_zone_stage(project_layout_dir, zone, date_id=date_id)
        color = STAGE_COLORS.get(stage)
        if color:
            zone_colors[zone] = color
    if date_id:
        available_zones = [zone for zone in available_zones if zone in zone_colors]
    img, _ = build_sonrisa_block_map(
        zone_bounds,
        available_zones,
        zone_colors=zone_colors,
        background_path=existing_map
    )
    if img is None:
        return jsonify({'error': 'Unable to build block map'}), 500
    buffer = BytesIO()
    img.save(buffer, format='PNG')
    buffer.seek(0)
    response = send_file(buffer, mimetype='image/png')
    response.headers['Cache-Control'] = 'no-store, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    return response


@app.route('/api/sonrisa/dates')
def get_sonrisa_dates():
    project = get_project_from_request()
    if project != "Sonrisa":
        return jsonify({'error': 'Invalid project'}), 400
    project_layout_dir = get_layout_dir(project)
    dates = get_sonrisa_all_dates(project_layout_dir)
    return jsonify({'dates': dates})


@app.route('/api/sonrisa/image/<zone>/<date_str>')
def get_sonrisa_image(zone, date_str):
    project = get_project_from_request()
    if project != "Sonrisa":
        return jsonify({'error': 'Invalid project'}), 400
    project_layout_dir = get_layout_dir(project)
    zone = normalize_zone_code(zone)
    if not zone:
        return jsonify({'error': 'Invalid zone'}), 400
    date_folder_path = find_sonrisa_date_folder(project_layout_dir, zone, date_str)
    if not date_folder_path:
        return jsonify({'error': f'Date folder not found for {zone} {date_str}'}), 404

    tif_candidates = [f for f in os.listdir(date_folder_path) if f.lower().endswith('.tif')]
    zone_tif = None
    zone_aliases = get_zone_aliases(zone)
    for alias in zone_aliases:
        target = f"{alias.lower()}_zone.tif"
        for f in tif_candidates:
            if f.lower() == target:
                zone_tif = f
                break
        if zone_tif:
            break
    if not zone_tif:
        for f in tif_candidates:
            if f.lower().endswith('_zone.tif'):
                zone_tif = f
                break
    if not zone_tif and tif_candidates:
        zone_tif = tif_candidates[0]
    if not zone_tif:
        return jsonify({'error': 'Zone TIFF not found'}), 404
    web_path = get_or_create_sonrisa_web_jpg(date_folder_path, zone_tif, max_dimension=4000)
    if web_path and os.path.exists(web_path):
        response = send_file(web_path, mimetype='image/jpeg')
        response.headers['Cache-Control'] = 'public, max-age=3600'
        return response

    tif_path = os.path.join(date_folder_path, zone_tif)

    try:
        Image.MAX_IMAGE_PIXELS = 2_000_000_000
        with rasterio.open(tif_path) as src:
            img_data = src.read()

        if len(img_data.shape) == 3:
            if img_data.shape[0] >= 3:
                img_array = np.transpose(img_data[:3], (1, 2, 0))
            elif img_data.shape[0] == 1:
                img_array = np.dstack([img_data[0], img_data[0], img_data[0]])
            else:
                img_array = np.transpose(img_data, (1, 2, 0))
        else:
            img_array = np.dstack([img_data, img_data, img_data])

        if img_array.dtype != np.uint8:
            img_array_normalized = np.zeros_like(img_array, dtype=np.uint8)
            for i in range(img_array.shape[2]):
                band = img_array[:, :, i]
                band_min = np.nanmin(band)
                band_max = np.nanmax(band)
                if band_max > band_min:
                    img_array_normalized[:, :, i] = ((band - band_min) / (band_max - band_min) * 255).astype(np.uint8)
            img_array = img_array_normalized

        img = Image.fromarray(img_array)
        max_size = 2000
        if max(img.width, img.height) > max_size:
            ratio = max_size / max(img.width, img.height)
            new_size = (int(img.width * ratio), int(img.height * ratio))
            img = img.resize(new_size, Image.LANCZOS)

        buffer = BytesIO()
        img.save(buffer, format='JPEG', quality=85, optimize=True)
        buffer.seek(0)
        return send_file(buffer, mimetype='image/jpeg')
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/tracker/<date_str>/<tracker_id>')
def get_tracker_image(date_str, tracker_id):
    """Get individual tracker TIFF as base64 for a specific date"""
    # Find tracker TIFF - try multiple locations
    tracker_tif = f"{tracker_id}_boundary_spine.tif"
    
    # Find the date folder to get the date_match
    project = get_project_from_request()
    project_layout_dir = get_layout_dir(project)
    if project == "Sonrisa":
        zone = normalize_zone_code(request.args.get('zone'))
        if not zone:
            return jsonify({'error': 'Zone required for Sonrisa'}), 400
        date_folder_path = find_sonrisa_date_folder(project_layout_dir, zone, date_str)
        if not date_folder_path:
            return jsonify({'error': f'Date folder not found for {zone} {date_str}'}), 404
    else:
        date_folder = f"{project}{date_str}"
        date_folder_path = os.path.join(project_layout_dir, date_folder)
    
    if not os.path.exists(date_folder_path):
        return jsonify({'error': f'Date folder not found'}), 404

    if project == "Sonrisa":
        if tracker_id.lower().endswith('.tif'):
            tracker_filename = tracker_id
        elif tracker_id.lower().endswith('_boundary_spine'):
            tracker_filename = f"{tracker_id}.tif"
        elif tracker_id.lower().endswith('_boundary'):
            tracker_filename = f"{tracker_id}.tif"
        else:
            tracker_filename = f"{tracker_id}_boundary_spine.tif"
        zone_folder_aliases = get_zone_folder_aliases(zone)
        tracker_paths = []
        for alias in zone_folder_aliases:
            tracker_paths.extend([
                os.path.join(date_folder_path, "trackers", alias, tracker_filename),
                os.path.join(date_folder_path, "trackers", alias, f"{tracker_id}.tif"),
                os.path.join(date_folder_path, "trackers", alias, f"{tracker_id}_boundary_spine.tif"),
                os.path.join(date_folder_path, "trackers", alias, f"{tracker_id}_boundary.tif"),
            ])
        tracker_paths.extend([
            os.path.join(date_folder_path, "trackers", tracker_filename),
            os.path.join(date_folder_path, tracker_filename),
            os.path.join(date_folder_path, f"{tracker_id}_boundary_spine.tif"),
            os.path.join(date_folder_path, f"{tracker_id}_boundary.tif"),
        ])
        for path in tracker_paths:
            if os.path.exists(path):
                base64_img = tif_to_base64(path)
                if base64_img:
                    return jsonify({'image': f'data:image/png;base64,{base64_img}'})
        return jsonify({'error': f'Tracker image not found: {tracker_id}'}), 404
    
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
        project = get_project_from_request()
        
        if not date_str:
            return jsonify({'error': 'Date required'}), 400
        
        # Find the date folder to get the date_match
        project_layout_dir = get_layout_dir(project)
        if project == "Sonrisa":
            zone = normalize_zone_code(request.args.get('zone'))
            if not zone:
                return jsonify({'error': 'Zone required for Sonrisa'}), 400
            date_folder_path = find_sonrisa_date_folder(project_layout_dir, zone, date_str)
        else:
            date_folder = f"{project}{date_str}"
            date_folder_path = os.path.join(project_layout_dir, date_folder)
        
        if not os.path.exists(date_folder_path):
            return jsonify({'error': 'Date folder not found'}), 404
        
        # Find any image file to get date_match
        # Try multiple patterns to find the date_match prefix
        base_image_files = [f for f in os.listdir(date_folder_path) 
                           if f.endswith('.jpg') and 
                           not f.endswith('_web.jpg')]
        
        if project != "Sonrisa" and not base_image_files:
            return jsonify({'error': f'No image files found for date {date_str}'}), 404
        
        # Extract date_match from the first image file found
        # Remove common suffixes to get base name
        date_match = None
        if base_image_files:
            first_image = base_image_files[0]
            date_match = first_image.replace('_stage_overlay.jpg', '').replace('_status_overlay.jpg', '').replace('_stage_status_overlay.jpg', '').replace('.jpg', '')
        
        # Try to find TIFF in the date folder first, then fall back to LEWISTIFS_DIR
        if project == "Sonrisa":
            tif_candidates = [f for f in os.listdir(date_folder_path) if f.lower().endswith('.tif')]
            zone_tif = None
            zone_aliases = get_zone_aliases(zone)
            for alias in zone_aliases:
                target = f"{alias.lower()}_zone.tif"
                for f in tif_candidates:
                    if f.lower() == target:
                        zone_tif = f
                        break
                if zone_tif:
                    break
            if not zone_tif:
                for f in tif_candidates:
                    if f.lower().endswith('_zone.tif'):
                        zone_tif = f
                        break
            if not zone_tif and tif_candidates:
                zone_tif = tif_candidates[0]
            if not zone_tif:
                return jsonify({'error': 'TIFF file not found'}), 404
            tif_path = os.path.join(date_folder_path, zone_tif)
        else:
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
            tif_crs = src.crs
        
        # Load boundaries
        if project == "Sonrisa":
            json_path = get_sonrisa_json_path(project_layout_dir)
        else:
            json_path = find_project_json(project_layout_dir, project)
        if not json_path or not os.path.exists(json_path):
            return jsonify({'error': 'JSON file not found'}), 404
        
        boundaries = load_tracker_boundaries(json_path)
        if project == "Sonrisa":
            normalized_boundaries = {}
            for tracker_id, b in boundaries.items():
                if normalize_zone_code(tracker_id) != zone:
                    continue
                normalized_id = normalize_tracker_id(tracker_id)
                if normalized_id:
                    reproj = reproject_boundary(b, "EPSG:4326", tif_crs)
                    normalized_boundaries[normalized_id] = reproj
            boundaries = normalized_boundaries
        
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
    print(f"Layout Directory: {BASE_LAYOUT_DIR}")
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

