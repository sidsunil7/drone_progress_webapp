from flask import Flask, render_template, send_file, jsonify, request, make_response, redirect
import logging
import os
import json
import csv
import mimetypes
import subprocess
import tempfile
import rasterio
from rasterio.warp import transform as rio_transform
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO
import base64
import re
import time
import hashlib
from urllib.parse import quote
from datetime import datetime, timezone, timedelta
from functools import lru_cache
import sys

# Configure root logger so ingest/watcher activity is visible in the terminal.
# Set LOG_LEVEL=DEBUG in the environment for more verbose output.
logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Ensure the project root is on sys.path so `db.*` imports work regardless of
# which directory the process is launched from.
_APP_ROOT = os.path.dirname(os.path.abspath(__file__))
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)

import atexit

from db.db_service import init_pool, close_pool
from db.repository import ProjectRepo, ManpowerRepo, TrackerRepo, FlightRepo, TrackerStatusRepo
from db.file_watcher import start_watcher, stop_watcher
from db.ingest_service import ingest_flight_folder, ingest_project, parse_folder

app = Flask(__name__)

# Initialise the PostgreSQL connection pool once at startup.
init_pool(minconn=1, maxconn=10)


def _clear_all_caches():
    """Clear every LRU cache in the application.

    Called by both /api/clear_cache and the file watcher after a successful
    auto-ingest so the UI reflects new data immediately.
    """
    # These are defined later in this module; the lambda defers the lookup.
    _cache_targets = [
        "_available_zones_cached",
        "_zone_bounds_cached",
        "_all_dates_cached",
        "_all_zone_stages_forward_fill_cached",
        "_all_zone_available_dates_cached",
        "_all_zone_stages_cached",
        "get_tracker_boundaries_cached",
        "build_sonrisa_layout_response_cached",
        "build_default_layout_response_cached",
        "get_tracker_info_cached",
        "get_tracker_info_json_cached",
    ]
    import sys
    module = sys.modules[__name__]
    for name in _cache_targets:
        fn = globals().get(name) or getattr(module, name, None)
        if fn and hasattr(fn, "cache_clear"):
            fn.cache_clear()

# Configuration - paths relative to project root
# For Railway deployment, use paths relative to app directory
BASE_DIR = os.environ.get('BASE_DIR', os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASE_LAYOUT_DIR = os.environ.get('LAYOUT_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), "layout_data"))
OUTPUT_DIR = os.environ.get('OUTPUT_DIR', os.path.join(BASE_DIR, "Output_Lewis"))
LEWISTIFS_DIR = os.environ.get('LEWISTIFS_DIR', os.path.join(BASE_DIR, "Lewistifs"))
TCPT_OBJDET_DIR = os.environ.get('TCPT_OBJDET_DIR', os.path.join(BASE_DIR, "tcpt_objdet_rawimg"))
SONRISA_JSON_PATH = os.environ.get(
    'SONRISA_JSON_PATH',
    os.path.join(BASE_LAYOUT_DIR, "Sonrisa", "Sonrisa_construction_AI.json")
)

ZONE_CODE_PATTERN = re.compile(r"^([A-Za-z])(\d{1,2})")
TIME_TOKEN_PATTERN = re.compile(r"^(\d{1,2})(am|pm)$", re.IGNORECASE)
STAGE_COLORS = {
    "pile": (200, 150, 255, 200),      # Light purple
    "torque_tube": (128, 0, 128, 200),  # Dark purple
    "module_rails": (135, 206, 250, 200),  # Light blue
    "solar_panel": (0, 0, 139, 200),    # Dark blue
}
DEFAULT_MAX_AGE = 3600
WEB_IMAGE_MAX_DIMENSION = int(os.environ.get("WEB_IMAGE_MAX_DIMENSION", "3000"))
WEBP_QUALITY = int(os.environ.get("WEBP_QUALITY", "72"))
JPEG_QUALITY = int(os.environ.get("JPEG_QUALITY", "82"))
APP_DATA_DIRNAME = "_app_data"
PROJECT_SETTINGS_FILENAME = "project_settings.json"
MANPOWER_DATA_FILENAME = "manpower_data.json"
DEFAULT_WORKING_DAYS = [1, 2, 3, 4, 5]
DEFAULT_HOURS_PER_DAY = 10
VALID_PROJECT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,79}$")
WEEKDAY_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
PRODUCTIVITY_STAGE_KEYS = ["pile", "torque_tube", "module_rails", "solar_panel"]
MARCH19_VIDEO_DATE = "20260319"
MARCH19_VIDEO_FOLDER = "Flight_18-20-31-32_20260319"
ZONE_VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm"}
HLS_MANIFEST_EXTENSIONS = {".m3u8"}
HLS_ASSET_EXTENSIONS = {".m3u8", ".ts", ".m4s", ".aac", ".mp4", ".vtt", ".key"}
ZONE_VIDEO_SOURCE_DIR = os.environ.get("ZONE_VIDEO_SOURCE_DIR", "")
ZONE_VIDEO_DEFAULT_SUBDIR = os.environ.get("ZONE_VIDEO_DEFAULT_SUBDIR", "Videos")
# Use the system temp dir so the cache is always on fast local disk,
# never on a network File Share mount (Azure App Service, etc.).
VIDEO_TRANSCODE_CACHE_DIR = os.environ.get(
    "VIDEO_TRANSCODE_CACHE_DIR",
    os.path.join(tempfile.gettempdir(), "drone_video_cache"),
)
# Set DISABLE_VIDEO_TRANSCODE=1 to skip ffprobe/ffmpeg entirely and serve
# the original file directly (useful when files are already H.264).
DISABLE_VIDEO_TRANSCODE = os.environ.get("DISABLE_VIDEO_TRANSCODE", "").strip() in ("1", "true", "yes")

# Video storage backend
# - filesystem: existing local/File Share discovery + Flask serving
# - blob: discover videos in Azure Blob and return SAS redirects for playback
# - blob_mount: discover/serve videos from a BlobFuse-style mounted path
VIDEO_STORAGE_BACKEND = os.environ.get("VIDEO_STORAGE_BACKEND", "filesystem").strip().lower()
AZURE_BLOB_ACCOUNT = os.environ.get("AZURE_BLOB_ACCOUNT", "").strip()
AZURE_BLOB_KEY = os.environ.get("AZURE_BLOB_KEY", "").strip()
AZURE_BLOB_CONTAINER = os.environ.get("AZURE_BLOB_CONTAINER", "layout-data").strip()
AZURE_BLOB_VIDEO_PREFIX = os.environ.get("AZURE_BLOB_VIDEO_PREFIX", "layout_data/{project}").strip()
AZURE_BLOB_SAS_EXPIRY_MINUTES = int(os.environ.get("AZURE_BLOB_SAS_EXPIRY_MINUTES", "60"))
AZURE_BLOB_MOUNT_ROOT = os.environ.get("AZURE_BLOB_MOUNT_ROOT", "").strip()
AZURE_BLOB_MOUNT_VIDEO_PREFIX = os.environ.get("AZURE_BLOB_MOUNT_VIDEO_PREFIX", "layout_data/{project}").strip()

# In-memory cache: abs_path -> (codec_name, codec_tag)
# Codec never changes for a given file, so caching for the server lifetime is safe.
_probe_cache: dict = {}
_blob_container_client = None
_blob_sdk_checked = False
_blob_sdk_available = False


# Start the filesystem watcher so new files in layout_data/ are automatically
# pushed to PostgreSQL and LRU caches are invalidated.
start_watcher(BASE_LAYOUT_DIR, _clear_all_caches)
atexit.register(stop_watcher)


@app.teardown_appcontext
def teardown_db(exception):
    """Return any borrowed DB connections to the pool at end of each request."""
    pass  # pool manages connections globally; nothing per-request to release


def log_timing(label, start_time, **context):
    """Print consistent timing logs for image/TIFF operations."""
    elapsed_ms = (time.perf_counter() - start_time) * 1000
    context_items = [f"{k}={v}" for k, v in context.items() if v is not None]
    context_suffix = f" ({', '.join(context_items)})" if context_items else ""
    print(f"[Timing] {label}: {elapsed_ms:.1f}ms{context_suffix}")


def build_etag(*parts):
    hasher = hashlib.sha1()
    for part in parts:
        hasher.update(str(part).encode("utf-8"))
        hasher.update(b"|")
    return f"\"{hasher.hexdigest()}\""


def request_etag_matches(etag):
    if_none_match = request.headers.get("If-None-Match", "")
    if not if_none_match:
        return False
    return etag in if_none_match or etag.strip('"') in if_none_match


def apply_cache_headers(response, etag, max_age=DEFAULT_MAX_AGE):
    response.headers["Cache-Control"] = f"public, max-age={max_age}"
    response.headers["ETag"] = etag
    return response


def make_not_modified_response(etag, max_age=DEFAULT_MAX_AGE):
    response = make_response("", 304)
    apply_cache_headers(response, etag, max_age=max_age)
    return response


def get_file_signature(file_path):
    """Return a lightweight change signature for cache invalidation."""
    stat = os.stat(file_path)
    return stat.st_mtime_ns, stat.st_size


def optional_file_signature(file_path):
    if not file_path or not os.path.exists(file_path):
        return (0, 0)
    return get_file_signature(file_path)


def probe_video_codec(file_path):
    """Run ffprobe to detect the video codec. Results are cached in _probe_cache."""
    if file_path in _probe_cache:
        return _probe_cache[file_path]
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_name,codec_tag_string",
                "-of", "json",
                file_path,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        payload = json.loads(result.stdout or "{}")
        streams = payload.get("streams") or []
        if not streams:
            _probe_cache[file_path] = (None, None)
            return None, None
        stream = streams[0] or {}
        result_val = (stream.get("codec_name"), stream.get("codec_tag_string"))
        _probe_cache[file_path] = result_val
        return result_val
    except Exception:
        _probe_cache[file_path] = (None, None)
        return None, None


def get_or_create_browser_compatible_video(source_path):
    """Return the path to serve for a video clip.

    Strategy:
    1. If DISABLE_VIDEO_TRANSCODE is set, serve the original immediately
       (use this when files are already H.264, e.g. pre-converted before upload).
    2. Build a stable cache key. If a transcoded H.264 copy already exists in
       the local-disk cache dir (system temp), serve it immediately — no
       ffprobe/ffmpeg needed. This is the fast path for all repeat requests.
    3. Otherwise probe the codec (result cached in _probe_cache for the server
       lifetime so ffprobe only runs once per unique file path).
       - If the codec is already browser-safe (H.264/etc.), serve the original.
       - If it needs transcoding (mp4v/mpeg4), run ffmpeg synchronously so we
         always serve the H.264 copy on this request. ffmpeg writes to the
         system temp dir, not the File Share, so it's fast on Azure.
    """
    if not source_path or not os.path.exists(source_path):
        return source_path

    if DISABLE_VIDEO_TRANSCODE:
        return source_path

    # Derive a stable cache path from the source file's identity.
    source_sig = get_file_signature(source_path)
    cache_key = build_etag("browser-video", source_path, source_sig).strip('"')
    os.makedirs(VIDEO_TRANSCODE_CACHE_DIR, exist_ok=True)
    target_path = os.path.join(VIDEO_TRANSCODE_CACHE_DIR, f"{cache_key}.mp4")

    # Fast path: transcoded copy already on local disk — serve with no I/O.
    if os.path.exists(target_path):
        return target_path

    # Probe codec once per file (result cached in memory).
    codec_name, codec_tag = probe_video_codec(source_path)
    needs_transcode = (codec_name == "mpeg4" or codec_tag == "mp4v")
    if not needs_transcode:
        return source_path

    # Transcode synchronously so the response is always H.264.
    # Writes to system temp dir (fast local SSD even on Azure App Service).
    # Keep the .mp4 extension on the temp file so ffmpeg can infer the container.
    tmp_path = target_path + ".tmp.mp4"
    cmd = [
        "ffmpeg", "-y", "-i", source_path,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-movflags", "+faststart",
        "-c:a", "aac", "-b:a", "128k",
        tmp_path,
    ]
    try:
        subprocess.run(cmd, capture_output=True, check=True, timeout=600)
        if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
            os.replace(tmp_path, target_path)
            return target_path
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    return source_path


def is_blob_video_backend_enabled():
    return VIDEO_STORAGE_BACKEND == "blob"


def is_blob_video_mount_backend_enabled():
    return VIDEO_STORAGE_BACKEND == "blob_mount"


def _blob_video_config_ready():
    return bool(AZURE_BLOB_ACCOUNT and AZURE_BLOB_KEY and AZURE_BLOB_CONTAINER)


def _ensure_blob_sdk_loaded():
    global _blob_sdk_checked, _blob_sdk_available
    if _blob_sdk_checked:
        return _blob_sdk_available
    try:
        from azure.storage.blob import BlobServiceClient, generate_blob_sas, BlobSasPermissions  # noqa: F401
        _blob_sdk_available = True
    except Exception:
        _blob_sdk_available = False
    _blob_sdk_checked = True
    return _blob_sdk_available


def _get_blob_container_client():
    global _blob_container_client
    if _blob_container_client is not None:
        return _blob_container_client
    if not (_blob_video_config_ready() and _ensure_blob_sdk_loaded()):
        return None
    try:
        from azure.storage.blob import BlobServiceClient
        account_url = f"https://{AZURE_BLOB_ACCOUNT}.blob.core.windows.net"
        service = BlobServiceClient(account_url=account_url, credential=AZURE_BLOB_KEY)
        _blob_container_client = service.get_container_client(AZURE_BLOB_CONTAINER)
    except Exception:
        _blob_container_client = None
    return _blob_container_client


def resolve_blob_video_prefix(project):
    base = (AZURE_BLOB_VIDEO_PREFIX or "layout_data/{project}").strip().strip("/")
    try:
        return base.format(project=project)
    except Exception:
        return base


def resolve_blob_mount_video_prefix(project):
    base = (AZURE_BLOB_MOUNT_VIDEO_PREFIX or "layout_data/{project}").strip().strip("/")
    try:
        return base.format(project=project)
    except Exception:
        return base


def normalize_blob_path(path_value):
    if not path_value or path_value.startswith(("/", "\\")):
        return None
    normalized = os.path.normpath(path_value).replace("\\", "/")
    if normalized in (".", ""):
        return None
    if normalized.startswith("../") or normalized == "..":
        return None
    return normalized


def _path_matches_prefix(prefix, normalized_path):
    if not normalized_path:
        return False
    check_prefix = str(prefix or "").strip("/")
    if not check_prefix:
        return False
    return normalized_path == check_prefix or normalized_path.startswith(f"{check_prefix}/")


def _blob_path_matches_project(project, normalized_blob_path):
    if not normalized_blob_path:
        return False
    prefix = resolve_blob_video_prefix(project).strip("/")
    return _path_matches_prefix(prefix, normalized_blob_path)


def _blob_mount_path_matches_project(project, normalized_path):
    if not normalized_path:
        return False
    prefix = resolve_blob_mount_video_prefix(project).strip("/")
    return _path_matches_prefix(prefix, normalized_path)


def _blob_mount_root_abs():
    root = (AZURE_BLOB_MOUNT_ROOT or "").strip()
    if not root:
        return None
    return os.path.abspath(root)


def _blob_mount_ready():
    root = _blob_mount_root_abs()
    return bool(root and os.path.isdir(root))


def build_blob_video_sas_url(blob_path):
    normalized_blob_path = normalize_blob_path(blob_path)
    if not normalized_blob_path:
        return None
    if not (_blob_video_config_ready() and _ensure_blob_sdk_loaded()):
        return None
    try:
        from azure.storage.blob import generate_blob_sas, BlobSasPermissions
        sas = generate_blob_sas(
            account_name=AZURE_BLOB_ACCOUNT,
            container_name=AZURE_BLOB_CONTAINER,
            blob_name=normalized_blob_path,
            account_key=AZURE_BLOB_KEY,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.now(timezone.utc) + timedelta(minutes=AZURE_BLOB_SAS_EXPIRY_MINUTES),
        )
        encoded_blob = quote(normalized_blob_path, safe="/")
        return f"https://{AZURE_BLOB_ACCOUNT}.blob.core.windows.net/{AZURE_BLOB_CONTAINER}/{encoded_blob}?{sas}"
    except Exception:
        return None


def blob_exists(blob_path):
    client = _get_blob_container_client()
    normalized_blob_path = normalize_blob_path(blob_path)
    if client is None or not normalized_blob_path:
        return False
    try:
        return client.get_blob_client(normalized_blob_path).exists()
    except Exception:
        return False


def read_blob_text(blob_path):
    client = _get_blob_container_client()
    normalized_blob_path = normalize_blob_path(blob_path)
    if client is None or not normalized_blob_path:
        return None
    try:
        blob_client = client.get_blob_client(normalized_blob_path)
        data = blob_client.download_blob().readall()
        return data.decode("utf-8", errors="ignore")
    except Exception:
        return None


def utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_project_name_input(raw_name):
    if raw_name is None:
        return None
    name = str(raw_name).strip()
    if not name or not VALID_PROJECT_NAME_PATTERN.fullmatch(name):
        return None
    return name


def parse_optional_iso_date(value):
    if value in (None, ""):
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def safe_float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def safe_int(value):
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def normalize_working_days(values):
    if values is None:
        return DEFAULT_WORKING_DAYS[:]
    if not isinstance(values, list):
        raise ValueError("working_days must be a list")
    normalized = []
    for value in values:
        day = safe_int(value)
        if day is None or day < 0 or day > 6:
            raise ValueError("working_days must contain integers from 0 to 6")
        if day not in normalized:
            normalized.append(day)
    if not normalized:
        raise ValueError("At least one working day is required")
    return sorted(normalized)


def working_day_labels(days):
    return [WEEKDAY_LABELS[day] for day in days if 0 <= day < len(WEEKDAY_LABELS)]


def read_json_file(file_path, default=None):
    if not file_path or not os.path.exists(file_path):
        return {} if default is None else default
    with open(file_path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def atomic_write_json(file_path, payload):
    parent_dir = os.path.dirname(file_path)
    os.makedirs(parent_dir, exist_ok=True)
    tmp_path = os.path.join(parent_dir, f".{os.path.basename(file_path)}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, file_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def derive_project_tracker_defaults_cached(project_name):
    """Return tracker defaults from DB (replaces construction_AI.json parsing)."""
    return TrackerRepo.get_tracker_defaults(project_name)


@lru_cache(maxsize=128)
def get_tracker_boundaries_cached(project_name, _cache_buster=None):
    """Return tracker boundaries from DB, keyed by project_name."""
    return TrackerRepo.get_boundaries(project_name)


@lru_cache(maxsize=256)
def get_tracker_info_cached(csv_path, csv_sig):
    return load_tracker_info(csv_path)


@lru_cache(maxsize=256)
def get_tracker_info_json_cached(json_path, json_sig):
    return load_tracker_info_json(json_path)


def get_tracker_info_from_sources(
    project_or_csv,
    zone_or_sig,
    csv_path=None,
    csv_sig=None,
    status_json_path=None,
    status_json_sig=None,
    folder_name_or_latest="latest",
):
    """Get tracker info, preferring DB lookup when project+zone are provided.

    Two calling conventions supported:
    1. New: (project_name, zone_code) — fetches from DB using latest available flight
    2. Old (file-based fallback): (csv_path, csv_sig, status_json_path, status_json_sig)
    """
    # If called with project_name + zone_code (new convention)
    if csv_path is None and status_json_path is None:
        project_name = project_or_csv
        zone_code = zone_or_sig
        return TrackerStatusRepo.get_tracker_info_by_folder_zone(
            project_name, folder_name_or_latest, zone_code
        )

    # Called with project+zone+file paths (hybrid — use DB, ignore file paths)
    project_name = project_or_csv
    zone_code = zone_or_sig
    result = TrackerStatusRepo.get_tracker_info_by_folder_zone(
        project_name, folder_name_or_latest, zone_code
    )
    if result:
        return result

    # Final fallback to file-based loading if DB returned nothing
    if csv_path and csv_sig and csv_sig != (0, 0):
        return get_tracker_info_cached(csv_path, csv_sig)
    if status_json_path and status_json_sig and status_json_sig != (0, 0):
        return get_tracker_info_json_cached(status_json_path, status_json_sig)
    return {}


@lru_cache(maxsize=256)
def get_tif_metadata_cached(tif_path, tif_sig):
    with rasterio.open(tif_path) as src:
        return list(src.transform), src.width, src.height, src.crs


@lru_cache(maxsize=256)
def get_synthetic_tif_metadata_cached(jpg_path, jpg_sig, min_lat, min_lon, max_lat, max_lon):
    """Synthesize TIF-like metadata from a plain JPG + zone geographic bounds.

    Used when a flight folder ships pre-rendered JPGs instead of raw GeoTIFFs.
    Returns the same 4-tuple as get_tif_metadata_cached so callers are agnostic.
    """
    with Image.open(jpg_path) as img:
        width, height = img.width, img.height
    lon_range = (max_lon - min_lon) or 1e-6
    lat_range = (max_lat - min_lat) or 1e-6
    a = lon_range / width
    e = -(lat_range / height)
    synthetic_transform = [a, 0.0, min_lon, 0.0, e, max_lat]
    return synthetic_transform, width, height, "EPSG:4326"


def is_identity_or_missing_georef(transform, crs):
    """Return True when raster metadata lacks usable georeferencing."""
    if not crs:
        return True
    if not transform or len(transform) < 6:
        return True
    a, b, c, d, e, f = transform[:6]
    return (
        abs(a - 1.0) < 1e-12
        and abs(b) < 1e-12
        and abs(c) < 1e-12
        and abs(d) < 1e-12
        and abs(e - 1.0) < 1e-12
        and abs(f) < 1e-12
    )


@lru_cache(maxsize=512)
def get_image_dimensions_cached(image_path, image_sig):
    with Image.open(image_path) as img:
        return img.width, img.height


@lru_cache(maxsize=128)
def encode_image_file_cached(image_path, image_sig, target_format, quality, max_dimension):
    Image.MAX_IMAGE_PIXELS = 2_000_000_000
    with Image.open(image_path) as img:
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        if max_dimension and max(img.width, img.height) > max_dimension:
            ratio = min(max_dimension / img.width, max_dimension / img.height)
            new_size = (int(img.width * ratio), int(img.height * ratio))
            img = img.resize(new_size, Image.LANCZOS)

        buffer = BytesIO()
        fmt = (target_format or "jpeg").lower()
        if fmt == "webp":
            img.save(buffer, format="WEBP", quality=quality, method=4, optimize=True)
            content_type = "image/webp"
        else:
            img.save(buffer, format="JPEG", quality=quality, optimize=True, progressive=True)
            content_type = "image/jpeg"
        return buffer.getvalue(), content_type, img.width, img.height


@lru_cache(maxsize=512)
def tif_to_base64_cached(tif_path, max_size, tif_sig):
    return tif_to_base64_uncached(tif_path, max_size=max_size)


_project_has_zones_cache = {}

def _get_dir_signature(dir_path):
    """Lightweight signature for a directory based on its mtime and child count."""
    try:
        st = os.stat(dir_path)
        children = os.listdir(dir_path)
        return (st.st_mtime_ns, len(children))
    except OSError:
        return (0, 0)


@lru_cache(maxsize=32)
def _zone_bounds_cached(project_name, _cache_buster=None):
    """Return zone bounds from DB keyed by project_name."""
    return TrackerRepo.get_zone_bounds(project_name)


@lru_cache(maxsize=32)
def _available_zones_cached(project_name, _cache_buster=None):
    """Return sorted tuple of zone codes from DB."""
    return tuple(FlightRepo.get_available_zones(project_name))


@lru_cache(maxsize=32)
def _all_dates_cached(project_name, _cache_buster=None):
    """Return sorted tuple of { date, folder, display } dicts from DB."""
    return tuple(FlightRepo.get_all_dates(project_name))


@lru_cache(maxsize=64)
def _all_zone_stages_cached(project_name, date_id, _cache_buster=None):
    """Return { zone_code: majority_stage } for all zones at exactly date_id from DB."""
    calc_start = time.perf_counter()
    if not project_name or not date_id:
        return {}
    result = TrackerStatusRepo.get_all_zone_stages(project_name, date_id)
    log_timing("zone_stage_calculation", calc_start, date=date_id, zones=len(result))
    return result


def _all_zone_available_dates_cached(project_name):
    """Return { zone_code: [(date_id, folder_name), ...] } from DB."""
    return FlightRepo.get_all_zone_available_dates(project_name)


def _zone_most_recent_folder_at_or_before(project_name, zone, date_id):
    """Return (most_recent_date_id, folder_name) for zone at or before date_id, or None."""
    return FlightRepo.get_latest_flight_for_zone(project_name, zone, date_id)


@lru_cache(maxsize=64)
def _all_zone_stages_forward_fill_cached(project_name, date_id, _cache_buster=None):
    """Return { zone_code: majority_stage } forward-filled to date_id from DB."""
    return TrackerStatusRepo.get_all_zone_stages_forward_filled(project_name, date_id)


def normalize_status(status):
    """Normalize status strings to snake_case like 'not_started', 'in_progress', 'completed'."""
    if not status:
        return ""
    return status.strip().lower().replace(" ", "_")


def _init_stage_status_matrix():
    return {
        stage: {"not_started": 0, "in_progress": 0, "completed": 0}
        for stage in PRODUCTIVITY_STAGE_KEYS
    }


def _accumulate_normalized_stage_progress(matrix, current_stage, current_status):
    """Accumulate one tracker across full stage sequence progression.

    Rules:
    - earlier stages than current stage -> completed
    - current stage -> current status
    - later stages -> not_started
    """
    stage_key = (current_stage or "").strip().lower().replace(" ", "_")
    if stage_key not in PRODUCTIVITY_STAGE_KEYS:
        return
    status_key = normalize_status(current_status) or "not_started"
    if status_key not in ("not_started", "in_progress", "completed"):
        status_key = "not_started"

    current_idx = PRODUCTIVITY_STAGE_KEYS.index(stage_key)
    for idx, stage in enumerate(PRODUCTIVITY_STAGE_KEYS):
        if idx < current_idx:
            mapped = "completed"
        elif idx > current_idx:
            mapped = "not_started"
        else:
            mapped = status_key
        matrix[stage][mapped] += 1


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
    match = ZONE_CODE_PATTERN.match(str(value).strip())
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
    # Normalize zone-letter prefix: G→A so "G18T01R01" and "A18T01R01" map to
    # the same canonical key.  Both can exist in dim_tracker when data was
    # ingested under different zone-code conventions (same root cause as the
    # "13" / "G13" zone_code duplicates).
    if len(t) >= 2 and t[0].upper() == "G" and t[1].isdigit():
        t = "A" + t[1:]
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
    """Return folder name aliases for a zone. Includes Zone_N format for Flight-style folders."""
    zone = normalize_zone_code(zone)
    if not zone:
        return []
    letter = zone[0].upper()
    number = zone[1:]
    try:
        compact_num = str(int(number))
    except ValueError:
        compact_num = number
    aliases = [f"{letter}{number}", f"{letter}{compact_num}"]
    # Flight-style folders use Zone_18, Zone_20, etc.
    aliases.extend([f"Zone_{number}", f"Zone_{compact_num}"])
    return list(dict.fromkeys(aliases))  # preserve order, remove dupes


def parse_sonrisa_folder_info(folder_name):
    if not folder_name:
        return [], None, None
    tokens = re.split(r"[_-]+", folder_name)
    zones = []
    current_letter = None
    date_str = None
    time_token = None
    past_zone_section = False
    for token in tokens:
        if date_str:
            time_match = TIME_TOKEN_PATTERN.match(token)
            if time_match:
                time_token = token.lower()
            continue
        if token.isdigit() and len(token) == 8:
            date_str = token
            continue
        # Only treat as past-zone tokens like "74m" (altitude) or "ovrlp", not words like "Flight"
        if (re.match(r"^\d+m$", token.lower()) or token.lower() in ("ovrlp", "overlp")):
            past_zone_section = True
            continue
        if past_zone_section:
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
    return ProjectRepo.list_projects()


def get_project_app_data_dir(project_layout_dir):
    return os.path.join(project_layout_dir, APP_DATA_DIRNAME)


def get_project_settings_path(project_layout_dir):
    return os.path.join(get_project_app_data_dir(project_layout_dir), PROJECT_SETTINGS_FILENAME)


def get_project_manpower_data_path(project_layout_dir):
    return os.path.join(get_project_app_data_dir(project_layout_dir), MANPOWER_DATA_FILENAME)


def ensure_project_scaffold(project_layout_dir):
    os.makedirs(get_project_app_data_dir(project_layout_dir), exist_ok=True)


def find_project_metadata_json(project_layout_dir, project_name):
    if not project_layout_dir or not os.path.isdir(project_layout_dir):
        return None
    preferred = os.path.join(project_layout_dir, f"{project_name}-NY_construction_AI_corrected_1.json")
    if os.path.exists(preferred):
        return preferred
    for fname in sorted(os.listdir(project_layout_dir)):
        lower = fname.lower()
        if lower.endswith("_construction_ai.json") or lower.endswith("_construction_ai_corrected_1.json"):
            return os.path.join(project_layout_dir, fname)
    for fname in sorted(os.listdir(project_layout_dir)):
        if fname.lower().endswith(".json"):
            return os.path.join(project_layout_dir, fname)
    return None


def get_project_date_entries(project):
    project_layout_dir = get_layout_dir(project)
    if not project_layout_dir or not os.path.isdir(project_layout_dir):
        return []
    if project_has_zones(project):
        return list(_all_dates_cached(project))

    dates = []
    for item in os.listdir(project_layout_dir):
        item_path = os.path.join(project_layout_dir, item)
        if os.path.isdir(item_path) and item.startswith(project):
            date_str = item.replace(project, "")
            if date_str.isdigit() and len(date_str) == 8:
                dates.append({
                    "date": date_str,
                    "folder": item,
                    "display": f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}",
                })
    dates.sort(key=lambda item: item["date"])
    return dates


def get_project_date_bounds(project):
    dates = get_project_date_entries(project)
    if not dates:
        return None, None
    return dates[0]["display"], dates[-1]["display"]


def derive_project_tracker_defaults(project_layout_dir, project_name):
    return derive_project_tracker_defaults_cached(project_name)


def build_project_settings_response(project_name):
    row = ProjectRepo.get_settings(project_name)
    if not row:
        return {
            "project_name": project_name,
            "working_days": DEFAULT_WORKING_DAYS[:],
            "working_day_labels": working_day_labels(DEFAULT_WORKING_DAYS),
            "hours_per_day": float(DEFAULT_HOURS_PER_DAY),
            "mw_per_tracker": None,
            "modules_per_tracker": None,
            "project_start_date": None,
            "project_end_date": None,
            "created_at": None,
            "updated_at": None,
            "settings_exists": False,
            "project_json_path": None,
            "total_project_trackers": None,
        }

    working_days = row.get("working_days") or DEFAULT_WORKING_DAYS
    try:
        normalized_working_days = normalize_working_days(working_days)
    except ValueError:
        normalized_working_days = DEFAULT_WORKING_DAYS[:]

    hours_per_day = safe_float(row.get("hours_per_day"))
    if hours_per_day is None or hours_per_day <= 0:
        hours_per_day = float(DEFAULT_HOURS_PER_DAY)

    start_date = row.get("start_date")
    end_date = row.get("end_date")
    start_str = start_date.strftime("%Y-%m-%d") if hasattr(start_date, "strftime") else str(start_date) if start_date else None
    end_str = end_date.strftime("%Y-%m-%d") if hasattr(end_date, "strftime") else str(end_date) if end_date else None

    created_at = row.get("created_at")
    updated_at = row.get("updated_at")
    created_str = created_at.strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(created_at, "strftime") else str(created_at) if created_at else None
    updated_str = updated_at.strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(updated_at, "strftime") else str(updated_at) if updated_at else None

    return {
        "project_name": project_name,
        "working_days": normalized_working_days,
        "working_day_labels": working_day_labels(normalized_working_days),
        "hours_per_day": hours_per_day,
        "mw_per_tracker": float(row["mw_per_tracker"]) if row.get("mw_per_tracker") is not None else None,
        "modules_per_tracker": row.get("modules_per_tracker"),
        "project_start_date": start_str,
        "project_end_date": end_str,
        "created_at": created_str,
        "updated_at": updated_str,
        "settings_exists": True,
        "project_json_path": None,
        "total_project_trackers": row.get("total_project_trackers") or None,
    }


def build_project_record(project_name):
    settings = build_project_settings_response(project_name)
    return {
        "name": project_name,
        "project_start_date": settings.get("project_start_date"),
        "project_end_date": settings.get("project_end_date"),
        "working_days": settings.get("working_days"),
        "working_day_labels": settings.get("working_day_labels"),
        "hours_per_day": settings.get("hours_per_day"),
        "mw_per_tracker": settings.get("mw_per_tracker"),
        "modules_per_tracker": settings.get("modules_per_tracker"),
        "settings_exists": settings.get("settings_exists"),
        "project_json_path": settings.get("project_json_path"),
    }


def get_project_records():
    return [build_project_record(project_name) for project_name in get_available_projects()]


def normalize_manual_dates(values):
    if values in (None, ""):
        return []
    if not isinstance(values, list):
        raise ValueError("manual_dates must be a list")
    normalized = []
    for value in values:
        parsed = parse_optional_iso_date(value)
        if not parsed:
            raise ValueError("manual_dates must contain YYYY-MM-DD strings")
        if parsed not in normalized:
            normalized.append(parsed)
    return sorted(normalized)


def normalize_actual_stage_dates(values):
    if values in (None, ""):
        values = {}
    if not isinstance(values, dict):
        raise ValueError("actual_stage_dates must be an object")
    normalized = {}
    for stage_key in PRODUCTIVITY_STAGE_KEYS:
        raw_value = values.get(stage_key)
        parsed = parse_optional_iso_date(raw_value)
        if raw_value not in (None, "") and not parsed:
            raise ValueError(f"actual_stage_dates.{stage_key} must use YYYY-MM-DD format.")
        normalized[stage_key] = parsed
    return normalized


def normalize_manpower_config(values):
    if values in (None, ""):
        return {}
    if not isinstance(values, dict):
        raise ValueError("manpower_config must be an object")

    normalized = {}
    for raw_date, raw_config in values.items():
        parsed_date = parse_optional_iso_date(raw_date)
        if not parsed_date:
            raise ValueError("manpower_config keys must use YYYY-MM-DD format.")
        if not isinstance(raw_config, dict):
            raise ValueError(f"manpower_config.{raw_date} must be an object")

        entry = {}
        total = 0
        for stage_key in PRODUCTIVITY_STAGE_KEYS:
            number = safe_float(raw_config.get(stage_key))
            if number is None:
                number = 0.0
            if number < 0:
                raise ValueError(f"manpower_config.{parsed_date}.{stage_key} must be greater than or equal to 0.")
            if float(number).is_integer():
                number = int(number)
            entry[stage_key] = number
            total += number
        entry["total"] = total
        normalized[parsed_date] = entry

    return dict(sorted(normalized.items()))


def validate_project_settings_payload(payload, *, require_project_name=False):
    cleaned = {}
    if require_project_name:
        project_name = normalize_project_name_input(payload.get("project_name"))
        if not project_name:
            raise ValueError("Project name is required and may only contain letters, numbers, spaces, underscores, and hyphens.")
        cleaned["project_name"] = project_name

    cleaned["working_days"] = normalize_working_days(payload.get("working_days", DEFAULT_WORKING_DAYS))

    hours_per_day = safe_float(payload.get("hours_per_day"))
    if hours_per_day is None or hours_per_day <= 0:
        raise ValueError("hours_per_day must be greater than 0.")
    cleaned["hours_per_day"] = hours_per_day

    project_start_date = parse_optional_iso_date(payload.get("project_start_date"))
    project_end_date = parse_optional_iso_date(payload.get("project_end_date"))
    if payload.get("project_start_date") not in (None, "") and not project_start_date:
        raise ValueError("project_start_date must use YYYY-MM-DD format.")
    if payload.get("project_end_date") not in (None, "") and not project_end_date:
        raise ValueError("project_end_date must use YYYY-MM-DD format.")
    if project_start_date and project_end_date and project_end_date < project_start_date:
        raise ValueError("project_end_date must be on or after project_start_date.")
    cleaned["project_start_date"] = project_start_date
    cleaned["project_end_date"] = project_end_date

    mw_per_tracker = safe_float(payload.get("mw_per_tracker"))
    modules_per_tracker = safe_int(payload.get("modules_per_tracker"))
    cleaned["mw_per_tracker"] = mw_per_tracker
    cleaned["modules_per_tracker"] = modules_per_tracker
    return cleaned


def validate_manpower_data_payload(payload):
    payload = payload or {}
    return {
        "manual_dates": normalize_manual_dates(payload.get("manual_dates", [])),
        "manpower_config": normalize_manpower_config(payload.get("manpower_config", {})),
        "actual_stage_dates": normalize_actual_stage_dates(payload.get("actual_stage_dates", {})),
    }


def save_project_settings(project_name, payload, *, creating=False):
    project_layout_dir = get_layout_dir(project_name)
    if creating:
        if ProjectRepo.get_project_id(project_name):
            raise FileExistsError(f"Project already exists: {project_name}")
        os.makedirs(project_layout_dir, exist_ok=True)
        ProjectRepo.create_project(project_name, payload)
    else:
        if not ProjectRepo.get_project_id(project_name):
            raise FileNotFoundError(f"Project not found: {project_name}")
        # mw_per_tracker and modules_per_tracker: prefer DB-derived values from
        # dim_tracker if present, otherwise use the submitted payload values.
        defaults = TrackerRepo.get_tracker_defaults(project_name)
        if defaults.get("mw_per_tracker") is not None:
            payload = dict(payload)
            payload["mw_per_tracker"] = defaults["mw_per_tracker"]
        if defaults.get("modules_per_tracker") is not None:
            payload = dict(payload)
            payload["modules_per_tracker"] = defaults["modules_per_tracker"]
        ProjectRepo.save_settings(project_name, payload)

    _project_has_zones_cache.pop(project_name, None)
    return build_project_settings_response(project_name)


def build_manpower_data_response(project_name):
    return ManpowerRepo.get_manpower(project_name)


def save_manpower_data(project_name, payload):
    if not ProjectRepo.get_project_id(project_name):
        raise FileNotFoundError(f"Project not found: {project_name}")
    return ManpowerRepo.save_manpower(project_name, payload)


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


def get_zone_json_path(project_layout_dir=None):
    """Find the construction JSON with zone bounds for any project.
    Falls back to Sonrisa-specific paths for backward compatibility."""
    if project_layout_dir:
        for fname in sorted(os.listdir(project_layout_dir)) if os.path.isdir(project_layout_dir) else []:
            if fname.lower().endswith("_construction_ai.json") or fname.lower().endswith("_construction_ai_corrected_1.json"):
                return os.path.join(project_layout_dir, fname)
    candidates = []
    if SONRISA_JSON_PATH:
        candidates.append(SONRISA_JSON_PATH)
    if project_layout_dir:
        candidates.append(os.path.join(project_layout_dir, "Sonrisa_construction_AI.json"))
    candidates.append(os.path.join(BASE_LAYOUT_DIR, "Sonrisa", "Sonrisa_construction_AI.json"))
    candidates.append(os.path.join(BASE_DIR, "Sonrisa_construction_AI.json"))
    for path in candidates:
        if path and os.path.exists(path):
            return path
    if project_layout_dir and os.path.isdir(project_layout_dir):
        for fname in os.listdir(project_layout_dir):
            if fname.lower().endswith(".json"):
                return os.path.join(project_layout_dir, fname)
    return None


get_sonrisa_json_path = get_zone_json_path


def project_has_zones(project):
    """Check whether a project uses zone-based layout (zones exist in DB)."""
    if project in _project_has_zones_cache:
        return _project_has_zones_cache[project]
    result = FlightRepo.has_zones(project)
    _project_has_zones_cache[project] = result
    return result


def get_sonrisa_zone_bounds(project_name_or_json_path):
    """Return zone bounds from DB for a project."""
    project_name = project_name_or_json_path
    return TrackerRepo.get_zone_bounds(project_name)


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

    # Flight-style: zones in Zone_N subfolders (e.g. Zone_18/tracker_status_v03.csv)
    if not csv_path and os.path.isdir(date_folder_path):
        csv_names = ["tracker_status_v03.csv", "tracker_status_v02.csv", "tracker_status.csv"]
        for alias in zone_folder_aliases:
            zone_subdir = alias if alias.startswith("Zone_") else f"Zone_{alias[1:]}"
            for name in csv_names:
                candidate = os.path.join(date_folder_path, zone_subdir, name)
                if os.path.exists(candidate):
                    csv_path = candidate
                    break
            if csv_path:
                break
        if not csv_path:
            for entry in os.listdir(date_folder_path):
                entry_path = os.path.join(date_folder_path, entry)
                if not os.path.isdir(entry_path):
                    continue
                if re.match(r"^Zone_(\d{1,2})$", entry, re.IGNORECASE):
                    for name in csv_names:
                        candidate = os.path.join(entry_path, name)
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


def find_sonrisa_zone_status_json(date_folder_path, zone):
    if not date_folder_path or not zone:
        return None
    json_names = ["zone_status.json", "tracker_status.json"]
    trackers_root = os.path.join(date_folder_path, "trackers")
    zone_folder_aliases = get_zone_folder_aliases(zone)

    if os.path.isdir(trackers_root):
        for alias in zone_folder_aliases:
            for name in json_names:
                candidate = os.path.join(trackers_root, alias, name)
                if os.path.exists(candidate):
                    return candidate
        for entry in os.listdir(trackers_root):
            for name in json_names:
                candidate = os.path.join(trackers_root, entry, name)
                if os.path.exists(candidate):
                    return candidate

    if os.path.isdir(date_folder_path):
        for alias in zone_folder_aliases:
            zone_subdirs = []
            if alias.lower().startswith("zone_"):
                zone_subdirs.append(alias)
            else:
                suffix = alias[1:]
                zone_subdirs.extend([f"Zone_{suffix}", f"zone_{suffix}"])
            for zone_subdir in zone_subdirs:
                for name in json_names:
                    candidate = os.path.join(date_folder_path, zone_subdir, name)
                    if os.path.exists(candidate):
                        return candidate
        for entry in os.listdir(date_folder_path):
            entry_path = os.path.join(date_folder_path, entry)
            if not os.path.isdir(entry_path):
                continue
            if re.match(r"^Zone_(\d{1,2})$", entry, re.IGNORECASE):
                for name in json_names:
                    candidate = os.path.join(entry_path, name)
                    if os.path.exists(candidate):
                        return candidate

    return None


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
        # folder is now a folder_name string; fetch tracker info from DB
        tracker_info = get_tracker_info_from_sources(project_layout_dir, zone)
        if not tracker_info:
            continue
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


def _extract_zone_codes_from_text(value):
    if not value:
        return []
    zones = set()
    for match in re.finditer(r"(?i)(?:^|[^A-Za-z0-9])zone[_-]?(\d{1,2})(?=[^A-Za-z0-9]|$)", value):
        normalized = normalize_zone_code(f"G{match.group(1)}")
        if normalized:
            zones.add(normalized)
    for match in re.finditer(r"(?i)(?:^|[^A-Za-z0-9])([ag])[_-]?(\d{1,2})(?=[^A-Za-z0-9]|$)", value):
        normalized = normalize_zone_code(f"{match.group(1)}{match.group(2)}")
        if normalized:
            zones.add(normalized)
    return sorted(zones)


def _resolve_zone_video_path(video_root, rel_path):
    if not rel_path or rel_path.startswith(("/", "\\")):
        return None, None
    normalized_rel = os.path.normpath(rel_path).replace("\\", "/")
    if normalized_rel.startswith("../") or normalized_rel == "..":
        return None, None
    target_path = os.path.normpath(os.path.join(video_root, normalized_rel))
    root_with_sep = os.path.join(os.path.abspath(video_root), "")
    if not os.path.abspath(target_path).startswith(root_with_sep):
        return None, None
    return target_path, normalized_rel


def _zone_matches_video_path(zone, rel_path):
    return zone in _extract_zone_codes_from_text(rel_path or "")


def _is_video_asset_dir(path):
    if not os.path.isdir(path):
        return False
    for root, _, files in os.walk(path):
        for filename in files:
            ext = os.path.splitext(filename)[1].lower()
            if ext in ZONE_VIDEO_EXTENSIONS or ext in HLS_MANIFEST_EXTENSIONS:
                return True
    return False


def resolve_zone_video_roots(project_layout_dir, date_str):
    if not project_layout_dir or not os.path.isdir(project_layout_dir):
        return []
    target_date, _ = split_sonrisa_date_time(date_str)
    if not target_date:
        return []
    roots = []
    seen = set()
    def _add_root(path):
        if not path:
            return
        abs_path = os.path.abspath(path)
        if abs_path in seen:
            return
        if _is_video_asset_dir(abs_path):
            seen.add(abs_path)
            roots.append(abs_path)

    source_dir_cfg = (ZONE_VIDEO_SOURCE_DIR or "").strip()
    if source_dir_cfg:
        configured = source_dir_cfg.format(date=target_date)
        candidate = configured if os.path.isabs(configured) else os.path.join(project_layout_dir, configured)
        _add_root(candidate)

    if target_date == MARCH19_VIDEO_DATE:
        candidate = os.path.join(project_layout_dir, MARCH19_VIDEO_FOLDER)
        _add_root(candidate)

    for item in sorted(os.listdir(project_layout_dir)):
        item_path = os.path.join(project_layout_dir, item)
        if not os.path.isdir(item_path):
            continue
        _, folder_date, _ = parse_sonrisa_folder_info(item.strip())
        if folder_date != target_date:
            continue
        preferred = os.path.join(item_path, ZONE_VIDEO_DEFAULT_SUBDIR)
        if _is_video_asset_dir(preferred):
            _add_root(item_path)
        elif _is_video_asset_dir(item_path):
            _add_root(item_path)
    return roots


def _video_rel_matches_date(rel_path, date_str):
    target_date, _ = split_sonrisa_date_time(date_str)
    if not target_date:
        return False
    normalized = str(rel_path or "").replace("\\", "/")
    if not normalized:
        return False
    for part in normalized.split("/"):
        if not part:
            continue
        _, folder_date, _ = parse_sonrisa_folder_info(part.strip())
        if folder_date == target_date:
            return True
    return False


def _is_blob_video_candidate_path(blob_name):
    normalized = str(blob_name or "").replace("\\", "/").strip("/")
    if not normalized:
        return False
    # Blob hierarchy stores videos under .../videos/ for this project.
    return "/videos/" in normalized.lower()


def discover_zone_videos_blob(project, date_str):
    if not is_blob_video_backend_enabled():
        return {}
    target_date, _ = split_sonrisa_date_time(date_str)
    if not target_date:
        return {}
    container_client = _get_blob_container_client()
    if container_client is None:
        return {}
    prefix = resolve_blob_video_prefix(project).strip("/")
    if not prefix:
        return {}
    by_zone = {}
    list_prefix = f"{prefix}/"
    try:
        for blob in container_client.list_blobs(name_starts_with=list_prefix):
            blob_name = normalize_blob_path(getattr(blob, "name", ""))
            if not blob_name:
                continue
            if not _is_blob_video_candidate_path(blob_name):
                continue
            if not _video_rel_matches_date(blob_name, date_str):
                continue
            _, ext = os.path.splitext(blob_name)
            ext = ext.lower()
            if ext not in ZONE_VIDEO_EXTENSIONS and ext not in HLS_MANIFEST_EXTENSIONS:
                continue
            zones = _extract_zone_codes_from_text(blob_name)
            if not zones:
                continue
            label = os.path.splitext(os.path.basename(blob_name))[0].replace("_", " ").strip()
            if ext in HLS_MANIFEST_EXTENSIONS:
                clip = {"kind": "hls", "manifest_path": blob_name, "label": label}
            else:
                clip = {"kind": "progressive", "clip_name": blob_name, "label": label}
            for zone in zones:
                by_zone.setdefault(zone, []).append(clip)
    except Exception:
        return {}
    for zone, clips in by_zone.items():
        clips.sort(key=lambda item: (0 if item.get("kind") == "hls" else 1, item.get("label") or ""))
    return by_zone


def _resolve_blob_mount_path(rel_path):
    mount_root = _blob_mount_root_abs()
    if not mount_root:
        return None, None
    return _resolve_zone_video_path(mount_root, rel_path)


def discover_zone_videos_blob_mount(project, date_str):
    if not is_blob_video_mount_backend_enabled():
        return {}
    target_date, _ = split_sonrisa_date_time(date_str)
    if not target_date:
        return {}
    mount_root = _blob_mount_root_abs()
    if not mount_root:
        return {}
    project_prefix = resolve_blob_mount_video_prefix(project).strip("/")
    if not project_prefix:
        return {}
    project_root_abs, _ = _resolve_zone_video_path(mount_root, project_prefix)
    if not project_root_abs or not os.path.isdir(project_root_abs):
        return {}
    by_zone = {}
    for root, _, files in os.walk(project_root_abs):
        for filename in sorted(files):
            _, ext = os.path.splitext(filename)
            ext = ext.lower()
            if ext not in ZONE_VIDEO_EXTENSIONS and ext not in HLS_MANIFEST_EXTENSIONS:
                continue
            full_path = os.path.join(root, filename)
            rel_path = os.path.relpath(full_path, mount_root).replace(os.sep, "/")
            if not _is_blob_video_candidate_path(rel_path):
                continue
            if not _video_rel_matches_date(rel_path, date_str):
                continue
            zones = _extract_zone_codes_from_text(rel_path)
            if not zones:
                continue
            label = os.path.splitext(os.path.basename(rel_path))[0].replace("_", " ").strip()
            if ext in HLS_MANIFEST_EXTENSIONS:
                clip = {"kind": "hls", "manifest_path": rel_path, "label": label}
            else:
                clip = {"kind": "progressive", "clip_name": rel_path, "label": label}
            for zone in zones:
                by_zone.setdefault(zone, []).append(clip)
    for zone, clips in by_zone.items():
        clips.sort(key=lambda item: (0 if item.get("kind") == "hls" else 1, item.get("label") or ""))
    return by_zone


def discover_zone_videos(project_layout_dir, date_str):
    video_roots = resolve_zone_video_roots(project_layout_dir, date_str)
    if not video_roots:
        return {}
    by_zone = {}
    for video_root in video_roots:
        for root, _, files in os.walk(video_root):
            for filename in sorted(files):
                _, ext = os.path.splitext(filename)
                ext = ext.lower()
                if ext not in ZONE_VIDEO_EXTENSIONS and ext not in HLS_MANIFEST_EXTENSIONS:
                    continue
                full_path = os.path.join(root, filename)
                rel_path = os.path.relpath(full_path, project_layout_dir).replace(os.sep, "/")
                if not _video_rel_matches_date(rel_path, date_str):
                    continue
                zones = _extract_zone_codes_from_text(rel_path)
                if not zones:
                    continue
                label = os.path.splitext(os.path.basename(rel_path))[0].replace("_", " ").strip()
                if ext in HLS_MANIFEST_EXTENSIONS:
                    clip = {"kind": "hls", "manifest_path": rel_path, "label": label}
                else:
                    clip = {"kind": "progressive", "clip_name": rel_path, "label": label}
                for zone in zones:
                    by_zone.setdefault(zone, []).append(clip)
    for zone, clips in by_zone.items():
        clips.sort(key=lambda item: (0 if item.get("kind") == "hls" else 1, item.get("label") or ""))
    return by_zone


@lru_cache(maxsize=128)
def discover_zone_videos_blob_cached(project, date_str):
    return discover_zone_videos_blob(project, date_str)


@lru_cache(maxsize=128)
def discover_zone_videos_blob_mount_cached(project, date_str):
    return discover_zone_videos_blob_mount(project, date_str)


@lru_cache(maxsize=128)
def discover_zone_videos_cached(project_layout_dir, date_str, dir_sig):
    return discover_zone_videos(project_layout_dir, date_str)


def _encode_rel_path_for_url(path_value):
    return "/".join(quote(part, safe="") for part in str(path_value or "").split("/") if part not in ("", "."))


def _rewrite_hls_manifest(manifest_text, zone, date_str, manifest_rel_path):
    base_dir = os.path.dirname(manifest_rel_path).replace("\\", "/")
    encoded_zone = quote(zone, safe="")
    encoded_date = quote(date_str, safe="")
    encoded_manifest = _encode_rel_path_for_url(manifest_rel_path)

    def to_asset_url(asset_ref):
        if not asset_ref:
            return asset_ref
        ref = asset_ref.strip()
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", ref):
            return ref
        joined = os.path.normpath(os.path.join(base_dir, ref)).replace("\\", "/")
        if joined.startswith("../") or joined == "..":
            return ref
        # Use query parameter for asset path to avoid ambiguity with multiple
        # path converters in Flask routes.
        encoded_asset = quote(ref, safe="")
        return f"/api/zone/video/hls_asset/{encoded_zone}/{encoded_date}/{encoded_manifest}?asset={encoded_asset}"

    rewritten_lines = []
    for line in manifest_text.splitlines():
        stripped = line.strip()
        if not stripped:
            rewritten_lines.append(line)
            continue
        if stripped.startswith("#"):
            if "URI=" in line:
                def _repl(match):
                    original = match.group(1)
                    return f'URI="{to_asset_url(original)}"'
                line = re.sub(r'URI="([^"]+)"', _repl, line)
            rewritten_lines.append(line)
            continue
        rewritten_lines.append(to_asset_url(stripped))
    return "\n".join(rewritten_lines) + ("\n" if manifest_text.endswith("\n") else "")


def find_prerendered_zone_jpg(folder_path, zone):
    """Return path to a pre-rendered zone web JPG if one exists in folder_path.

    New-format flight folders ship G31_zone_web.jpg directly instead of raw TIFs.
    """
    if not folder_path or not os.path.isdir(folder_path):
        return None
    for alias in get_zone_aliases(zone):
        candidate = os.path.join(folder_path, f"{alias.lower()}_zone_web.jpg")
        if os.path.exists(candidate):
            return candidate
    return None


def find_sonrisa_zone_tif_fallback(project_layout_dir, zone):
    """Find zone TIFF and web JPG from another folder (e.g. G-style) when current folder (e.g. Flight-style) lacks them."""
    zone = normalize_zone_code(zone)
    if not zone or not project_layout_dir or not os.path.exists(project_layout_dir):
        return None, None, None
    zone_aliases = get_zone_aliases(zone)
    for item in sorted(os.listdir(project_layout_dir)):
        item_path = os.path.join(project_layout_dir, item)
        if not os.path.isdir(item_path):
            continue
        folder_zones, _, _ = parse_sonrisa_folder_info(item.strip())
        if zone not in folder_zones:
            continue
        tif_candidates = [f for f in os.listdir(item_path) if f.lower().endswith('.tif')]
        zone_tif = None
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
        if not zone_tif:
            continue
        tif_path = os.path.join(item_path, zone_tif)
        web_path = get_or_create_sonrisa_web_jpg(item_path, zone_tif, max_dimension=4000)
        if os.path.exists(tif_path):
            return tif_path, zone_tif, web_path
    return None, None, None


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


def load_tracker_boundaries(project_name):
    """Load tracker boundaries from DB."""
    return TrackerRepo.get_boundaries(project_name)


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

    When format 2 or 3 is used (CSV lacks Current_stage and Status), the computed values
    are persisted back to the CSV so table, charts, and settings tabs can use them.
    """
    tracker_info = {}
    if not csv_path or not os.path.exists(csv_path):
        return tracker_info

    with open(csv_path, 'r', newline='') as csvfile:
        reader = csv.DictReader(csvfile)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

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

    need_persist = False
    rows_to_write = []

    for row in rows:
        tracker_id = (
            row.get('Tracker ID')
            or row.get('tracker_id')
            or row.get('trackerID')
            or ''
        ).strip()

        if has_current_cols:
            if tracker_id:
                stage = (row.get('Current_stage') or '').strip()
                status = (row.get('Status') or '').strip()
                tracker_info[tracker_id] = {'stage': stage, 'status': status}
            rows_to_write.append((row, None, None))
        elif has_per_stage_cols:
            if tracker_id:
                stage, status = compute_current_stage_from_row(row)
                tracker_info[tracker_id] = {'stage': stage, 'status': status}
                need_persist = True
                rows_to_write.append((row, stage, status))
            else:
                rows_to_write.append((row, None, None))
        elif has_installation_cols:
            if tracker_id:
                stage, status = compute_current_stage_from_installation_row(row)
                tracker_info[tracker_id] = {'stage': stage, 'status': status}
                need_persist = True
                rows_to_write.append((row, stage, status))
            else:
                rows_to_write.append((row, None, None))
        else:
            rows_to_write.append((row, None, None))

    # Persist Current_stage and Status to CSV when computed from per-stage or installation columns
    if need_persist and rows_to_write:
        _persist_current_stage_to_csv(csv_path, fieldnames, rows_to_write)

    return tracker_info


def load_tracker_info_json(json_path):
    """Load tracker stage and status from zone_status.json."""
    tracker_info = {}
    if not json_path or not os.path.exists(json_path):
        return tracker_info
    data = read_json_file(json_path, default={})
    trackers = data.get("trackers", {})
    if not isinstance(trackers, dict):
        return tracker_info
    for tracker_id, row in trackers.items():
        if not tracker_id or not isinstance(row, dict):
            continue
        stage, status = compute_current_stage_from_installation_row(row)
        tracker_info[tracker_id] = {"stage": stage, "status": status}
    return tracker_info


def _persist_current_stage_to_csv(csv_path, fieldnames, rows_with_computed):
    """Write CSV with Current_stage and Status columns added. Preserves existing columns."""
    if "Current_stage" not in fieldnames:
        fieldnames = list(fieldnames) + ["Current_stage"]
    if "Status" not in fieldnames:
        fieldnames = list(fieldnames) + ["Status"]

    try:
        with open(csv_path, 'w', newline='') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames, extrasaction='ignore')
            writer.writeheader()
            for row, stage, status in rows_with_computed:
                if stage is not None and status is not None:
                    row = dict(row)
                    row["Current_stage"] = stage
                    row["Status"] = status
                writer.writerow(row)
    except Exception as e:
        print(f"Warning: could not persist Current_stage/Status to {csv_path}: {e}")

def tif_to_base64_uncached(tif_path, max_size=2000):
    """Convert TIFF to base64 PNG for web display"""
    convert_start = time.perf_counter()
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
            log_timing(
                "tif_to_base64",
                convert_start,
                tif=os.path.basename(tif_path),
                width=img.width,
                height=img.height,
            )
            return base64.b64encode(buffer.getvalue()).decode('utf-8')
    except Exception as e:
        log_timing("tif_to_base64_failed", convert_start, tif=os.path.basename(tif_path))
        print(f"Error converting TIFF: {e}")
        import traceback
        traceback.print_exc()
        return None


def tif_to_base64(tif_path, max_size=2000):
    """Convert TIFF to base64 PNG for web display with file-change-aware caching."""
    try:
        tif_sig = get_file_signature(tif_path)
    except OSError:
        return None
    return tif_to_base64_cached(tif_path, max_size, tif_sig)


def image_to_base64(path, max_size=2000):
    """Convert image (PNG/JPG/TIFF) to base64 PNG for web display."""
    if not path or not os.path.exists(path):
        return None
    ext = os.path.splitext(path)[1].lower()
    if ext in ('.tif', '.tiff'):
        return tif_to_base64(path, max_size)
    if ext in ('.png', '.jpg', '.jpeg'):
        try:
            with Image.open(path) as img:
                img = img.convert('RGB')
                if max(img.width, img.height) > max_size:
                    ratio = max_size / max(img.width, img.height)
                    img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)
                buffer = BytesIO()
                img.save(buffer, format='PNG')
                return base64.b64encode(buffer.getvalue()).decode('utf-8')
        except Exception as e:
            print(f"Error converting image {path}: {e}")
            return None
    return None


@lru_cache(maxsize=128)
def build_sonrisa_layout_response_cached(
    date_str,
    zone,
    project,
    tif_path,
    tif_sig,
    json_path,
    json_sig,
    csv_path,
    csv_sig,
    status_json_path,
    status_json_sig,
    web_path,
    web_sig,
):
    calc_start = time.perf_counter()
    tif_meta_start = time.perf_counter()
    zone_bounds_map = _zone_bounds_cached(project)
    zb = zone_bounds_map.get(zone) or [0.0, 0.0, 1.0, 1.0]
    min_lat, min_lon, max_lat, max_lon = float(zb[0]), float(zb[1]), float(zb[2]), float(zb[3])
    try:
        transform, width, height, tif_crs = get_tif_metadata_cached(tif_path, tif_sig)
        if is_identity_or_missing_georef(transform, tif_crs):
            transform, width, height, tif_crs = get_synthetic_tif_metadata_cached(
                tif_path, tif_sig, min_lat, min_lon, max_lat, max_lon
            )
    except Exception:
        # tif_path is a pre-rendered JPG (new folder format) — synthesize transform from zone bounds.
        transform, width, height, tif_crs = get_synthetic_tif_metadata_cached(
            tif_path, tif_sig, min_lat, min_lon, max_lat, max_lon
        )
    log_timing("layout_calc_tif_metadata", tif_meta_start, date=date_str, zone=zone)

    boundaries_start = time.perf_counter()
    boundaries_raw = get_tracker_boundaries_cached(project)
    boundaries = {}
    for tracker_id, boundary in boundaries_raw.items():
        if normalize_zone_code(tracker_id) != zone:
            continue
        normalized_id = normalize_tracker_id(tracker_id)
        if normalized_id:
            boundaries[normalized_id] = reproject_boundary(boundary, "EPSG:4326", tif_crs)
    log_timing(
        "layout_calc_boundaries",
        boundaries_start,
        date=date_str,
        zone=zone,
        boundaries=len(boundaries),
    )

    tracker_info = {}
    best = _zone_most_recent_folder_at_or_before(project, zone, date_str)
    folder_name = best[1] if best else "latest"
    tracker_info_raw = get_tracker_info_from_sources(
        project,
        zone,
        csv_path,
        csv_sig,
        status_json_path,
        status_json_sig,
        folder_name_or_latest=folder_name,
    )
    if tracker_info_raw:
        tracker_info_start = time.perf_counter()
        for tracker_id, info in tracker_info_raw.items():
            normalized_id = normalize_tracker_id(tracker_id)
            # Only include tracker statuses for trackers that are present in the
            # zone boundary map for this layout. This avoids cross-zone/status
            # leakage if source data is inconsistent and keeps overlay colors aligned.
            if normalized_id and normalized_id in boundaries:
                tracker_info[normalized_id] = info
        log_timing(
            "layout_calc_tracker_info",
            tracker_info_start,
            date=date_str,
            zone=zone,
            trackers=len(tracker_info),
        )

    display_width = None
    display_height = None
    if web_path and web_sig != (0, 0):
        display_size_start = time.perf_counter()
        display_width, display_height = get_image_dimensions_cached(web_path, web_sig)
        # Keep overlay math aligned with /api/zone/image which may downscale the
        # encoded response to WEB_IMAGE_MAX_DIMENSION.
        if WEB_IMAGE_MAX_DIMENSION and max(display_width, display_height) > WEB_IMAGE_MAX_DIMENSION:
            resize_ratio = min(
                WEB_IMAGE_MAX_DIMENSION / display_width,
                WEB_IMAGE_MAX_DIMENSION / display_height,
            )
            display_width = max(1, int(display_width * resize_ratio))
            display_height = max(1, int(display_height * resize_ratio))
        log_timing(
            "layout_calc_display_dimensions",
            display_size_start,
            date=date_str,
            zone=zone,
            width=display_width,
            height=display_height,
        )
    if not display_width or not display_height:
        display_width, display_height, _ = get_display_dimensions(width, height, max_size=2000)

    sfx = (width / display_width) if display_width else 1.0
    sfy = (height / display_height) if display_height else 1.0

    response_data = {
        'boundaries': boundaries,
        'tracker_info': tracker_info,
        'transform': transform,
        'tif_width': width,
        'tif_height': height,
        'base_image': f'/api/zone/image/{zone}/{date_str}',
        'original_image_width': width,
        'original_image_height': height,
        'display_image_width': display_width,
        'display_image_height': display_height,
        'image_scale_factor': sfx,
        'image_scale_factor_x': sfx,
        'image_scale_factor_y': sfy,
        'date': date_str
    }
    log_timing(
        "layout_calc_response_build",
        calc_start,
        date=date_str,
        zone=zone,
        trackers=len(tracker_info),
        boundaries=len(boundaries),
    )
    return response_data


@lru_cache(maxsize=128)
def build_default_layout_response_cached(
    date_str,
    project,
    tif_path,
    tif_sig,
    json_path,
    json_sig,
    csv_path,
    csv_sig,
    base_image_path,
    base_image_sig,
    display_image_path,
    display_image_sig,
    base_image_name,
    overlay_image_name,
):
    calc_start = time.perf_counter()
    tif_meta_start = time.perf_counter()
    transform, width, height, _ = get_tif_metadata_cached(tif_path, tif_sig)
    log_timing("layout_calc_tif_metadata", tif_meta_start, date=date_str, project=project)

    boundaries_start = time.perf_counter()
    boundaries = get_tracker_boundaries_cached(project)
    log_timing(
        "layout_calc_boundaries",
        boundaries_start,
        date=date_str,
        project=project,
        boundaries=len(boundaries),
    )
    tracker_info = {}
    if csv_path and csv_sig != (0, 0):
        tracker_info_start = time.perf_counter()
        tracker_info = get_tracker_info_cached(csv_path, csv_sig)
        log_timing(
            "layout_calc_tracker_info",
            tracker_info_start,
            date=date_str,
            project=project,
            trackers=len(tracker_info),
        )

    original_width = None
    original_height = None
    display_width = None
    display_height = None
    scale_factor = 1.0

    if base_image_path and base_image_sig != (0, 0):
        image_dims_start = time.perf_counter()
        original_width, original_height = get_image_dimensions_cached(base_image_path, base_image_sig)
        if display_image_path and display_image_sig != (0, 0):
            display_width, display_height = get_image_dimensions_cached(display_image_path, display_image_sig)
            if display_width:
                scale_factor = original_width / display_width
        else:
            display_width = original_width
            display_height = original_height
        log_timing(
            "layout_calc_display_dimensions",
            image_dims_start,
            date=date_str,
            project=project,
            width=display_width,
            height=display_height,
        )

    sfx = scale_factor
    sfy = (
        (original_height / display_height)
        if (display_height and original_height)
        else scale_factor
    )

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
        'image_scale_factor': sfx,
        'image_scale_factor_x': sfx,
        'image_scale_factor_y': sfy,
        'date': date_str
    }
    if overlay_image_name:
        response_data['overlay_image'] = f'/api/image/layout/{date_str}/{overlay_image_name}'
    log_timing(
        "layout_calc_response_build",
        calc_start,
        date=date_str,
        project=project,
        trackers=len(tracker_info),
        boundaries=len(boundaries),
    )
    return response_data


def build_date_summary_cached(project_name, folder_name, zone_code, csv_path="", csv_sig=(0, 0), status_json_path="", status_json_sig=(0, 0)):
    """Build lightweight per-date summary data from DB (with file fallback)."""
    flight_id = FlightRepo.get_flight_id(project_name, folder_name) if folder_name else None
    zone_id = FlightRepo.get_zone_id(project_name, zone_code) if zone_code else None
    if flight_id and zone_id:
        return TrackerStatusRepo.get_date_summary(flight_id, zone_id)

    # Fallback: build from tracker_info dict
    tracker_info = get_tracker_info_from_sources(
        project_name, zone_code, csv_path or None, csv_sig, status_json_path or None, status_json_sig
    )
    summary = {
        "stageStatusCounts": {},
        "totalTrackers": len(tracker_info),
        "trackerStages": [],
    }
    for info in tracker_info.values():
        stage_key = (info.get("stage") or "").lower().replace(" ", "_")
        status_key = (info.get("status") or "").lower().replace(" ", "_")
        if stage_key:
            summary["trackerStages"].append({"stage": stage_key, "status": status_key})
        if not summary["stageStatusCounts"].get(stage_key):
            summary["stageStatusCounts"][stage_key] = {}
        if not summary["stageStatusCounts"][stage_key].get(status_key):
            summary["stageStatusCounts"][stage_key][status_key] = 0
        summary["stageStatusCounts"][stage_key][status_key] += 1
    return summary

@app.route('/')
def select_project():
    """Project selection page"""
    projects = get_project_records()
    return render_template('select_project.html', projects=projects)


@app.route('/project/<project>')
def project_home(project):
    """Set project selection and render the main app"""
    resolved_project = resolve_project_name(project)
    if not resolved_project:
        return render_template('select_project.html', projects=get_project_records()), 404
    has_zones = project_has_zones(resolved_project)
    response = make_response(render_template(
        'index.html', project=resolved_project, has_zones=has_zones
    ))
    response.set_cookie('project', resolved_project, samesite='Lax')
    return response


@app.route('/api/projects', methods=['GET', 'POST'])
def api_projects():
    if request.method == 'GET':
        return jsonify({'projects': get_project_records()})

    payload = request.get_json(silent=True) or {}
    try:
        cleaned = validate_project_settings_payload(payload, require_project_name=True)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400

    project_name = cleaned.pop("project_name")
    if resolve_project_name(project_name):
        return jsonify({'error': f'Project already exists: {project_name}'}), 409

    try:
        settings = save_project_settings(project_name, cleaned, creating=True)
    except FileExistsError as exc:
        return jsonify({'error': str(exc)}), 409
    except OSError as exc:
        return jsonify({'error': f'Unable to create project: {exc}'}), 500

    return jsonify({
        'project': build_project_record(project_name),
        'settings': settings,
    }), 201


@app.route('/api/project_settings', methods=['GET', 'PUT'])
def api_project_settings():
    project_param = request.args.get('project')
    if request.method == 'GET':
        project_name = resolve_project_name(project_param) if project_param else get_project_from_request()
        if not project_name:
            return jsonify({'error': 'Project not found'}), 404
        return jsonify(build_project_settings_response(project_name))

    payload = request.get_json(silent=True) or {}
    project_name = resolve_project_name(project_param or payload.get('project_name'))
    if not project_name:
        return jsonify({'error': 'Project not found'}), 404

    try:
        cleaned = validate_project_settings_payload(payload)
        settings = save_project_settings(project_name, cleaned, creating=False)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    except FileNotFoundError as exc:
        return jsonify({'error': str(exc)}), 404
    except OSError as exc:
        return jsonify({'error': f'Unable to save project settings: {exc}'}), 500

    return jsonify(settings)


@app.route('/api/manpower_data', methods=['GET', 'PUT'])
def api_manpower_data():
    project_param = request.args.get('project')
    if request.method == 'GET':
        project_name = resolve_project_name(project_param) if project_param else get_project_from_request()
        if not project_name:
            return jsonify({'error': 'Project not found'}), 404
        return jsonify(build_manpower_data_response(project_name))

    payload = request.get_json(silent=True) or {}
    project_name = resolve_project_name(project_param or payload.get('project_name'))
    if not project_name:
        project_name = get_project_from_request()
    if not project_name:
        return jsonify({'error': 'Project not found'}), 404

    try:
        cleaned = validate_manpower_data_payload(payload)
        data = save_manpower_data(project_name, cleaned)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    except FileNotFoundError as exc:
        return jsonify({'error': str(exc)}), 404
    except OSError as exc:
        return jsonify({'error': f'Unable to save manpower data: {exc}'}), 500

    return jsonify(data)


@app.route('/api/date_summary/<date_str>')
def get_date_summary(date_str):
    """Return lightweight summary data for charts/tables without full layout work."""
    request_start = time.perf_counter()
    project = get_project_from_request()
    project_layout_dir = get_layout_dir(project)
    is_zone_project = project_has_zones(project)

    if is_zone_project:
        zone = normalize_zone_code(request.args.get('zone'))
        if not zone:
            return jsonify({'error': 'Zone required for this project'}), 400
        best = _zone_most_recent_folder_at_or_before(project, zone, date_str)
        if not best:
            return jsonify({'error': f'Date folder not found for {zone} {date_str}'}), 404
        folder_name = best[1]
        calc_start = time.perf_counter()
        summary = build_date_summary_cached(project, folder_name, zone)
        log_timing(
            "date_summary_calc",
            calc_start,
            date=date_str,
            project=project,
            zone=zone,
            trackers=summary["totalTrackers"],
        )
        response = jsonify(summary)
        log_timing("get_date_summary", request_start, date=date_str, project=project, zone=zone)
        return response

    # Non-zone project: fall back to DB lookup by date
    calc_start = time.perf_counter()
    summary = build_date_summary_cached(project, None, None)
    log_timing(
        "date_summary_calc",
        calc_start,
        date=date_str,
        project=project,
        trackers=summary["totalTrackers"],
    )
    response = jsonify(summary)
    log_timing("get_date_summary", request_start, date=date_str, project=project)
    return response

@app.route('/api/dates')
def get_dates():
    """Get list of available dates from subfolders"""
    project = get_project_from_request()
    project_layout_dir = get_layout_dir(project)
    if not os.path.exists(project_layout_dir):
        return jsonify({'error': 'Layout directory not found'}), 404

    if project_has_zones(project):
        zone = normalize_zone_code(request.args.get('zone'))
        if not zone:
            return jsonify({'error': 'Zone required for this project'}), 400
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
    request_start = time.perf_counter()
    # Find the date folder
    project = get_project_from_request()
    project_layout_dir = get_layout_dir(project)
    is_zone_project = project_has_zones(project)
    if is_zone_project:
        zone = normalize_zone_code(request.args.get('zone'))
        if not zone:
            return jsonify({'error': 'Zone required for this project'}), 400
        date_folder_path = find_sonrisa_date_folder(project_layout_dir, zone, date_str)
        if not date_folder_path:
            return jsonify({'error': f'Date folder not found for {zone} {date_str}'}), 404
    else:
        date_folder = f"{project}{date_str}"
        date_folder_path = os.path.join(project_layout_dir, date_folder)
    
    if not os.path.exists(date_folder_path):
        if is_zone_project:
            return jsonify({'error': f'Date folder not found for {project} {date_str}'}), 404
        return jsonify({'error': f'Date folder not found: {date_folder}'}), 404

    if is_zone_project:
        tif_candidates = [f for f in os.listdir(date_folder_path) if f.lower().endswith('.tif')]
        zone_tif = None
        web_path = None
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
            # Flight-style folders lack zone TIFF; fall back to G-style folder
            fallback_tif_path, fallback_zone_tif, fallback_web_path = find_sonrisa_zone_tif_fallback(project_layout_dir, zone)
            if fallback_tif_path and fallback_zone_tif:
                zone_tif = fallback_zone_tif
                tif_path = fallback_tif_path
                web_path = fallback_web_path
            else:
                # New folder format: no TIF available; use pre-rendered JPG as image source.
                prerendered = find_prerendered_zone_jpg(date_folder_path, zone)
                if not prerendered:
                    return jsonify({'error': 'Zone TIFF not found'}), 404
                tif_path = prerendered
                web_path = prerendered
                zone_tif = os.path.basename(prerendered)
        else:
            tif_path = os.path.join(date_folder_path, zone_tif)

        tif_meta_start = time.perf_counter()
        tif_sig = get_file_signature(tif_path)
        # Warm the cache; for GeoTIFFs rasterio reads metadata; for JPGs the
        # synthetic-transform path in build_sonrisa_layout_response_cached handles it.
        try:
            _, _, _, _ = get_tif_metadata_cached(tif_path, tif_sig)
        except Exception:
            pass
        log_timing("layout_tif_metadata", tif_meta_start, date=date_str, zone=zone, tif=zone_tif)

        # json_path/json_sig are kept for lru_cache key compatibility but boundaries come from DB
        json_path = ""
        json_sig = (0, 0)
        csv_path = ""
        csv_sig = (0, 0)
        status_json_path = ""
        status_json_sig = (0, 0)

        if not web_path:
            web_path = get_or_create_sonrisa_web_jpg(
                os.path.dirname(tif_path), zone_tif, max_dimension=4000
            )
        web_sig = optional_file_signature(web_path)
        layout_etag = build_etag(
            "layout",
            "tracker_info_v2",
            project,
            date_str,
            zone,
            tif_sig,
            json_sig,
            csv_sig,
            status_json_sig,
            web_sig,
        )
        if request_etag_matches(layout_etag):
            return make_not_modified_response(layout_etag)

        response_data = build_sonrisa_layout_response_cached(
            date_str,
            zone,
            project,
            tif_path,
            tif_sig,
            json_path,
            json_sig,
            csv_path or "",
            csv_sig,
            status_json_path or "",
            status_json_sig,
            web_path or "",
            web_sig,
        )
        response = jsonify(response_data)
        apply_cache_headers(response, layout_etag)
        log_timing("get_layout_data", request_start, date=date_str, zone=zone, project=project)
        return response
    
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
    
    json_sig = get_file_signature(json_path)
    csv_sig = optional_file_signature(csv_path)
    
    # Get TIFF transform info
    # Try to find TIFF in the date folder first, then fall back to LEWISTIFS_DIR
    tif_path = os.path.join(date_folder_path, f"{date_match}.tif")
    if not os.path.exists(tif_path):
        # Fall back to LEWISTIFS_DIR
        tif_path = os.path.join(LEWISTIFS_DIR, f"{date_match}.tif")
        if not os.path.exists(tif_path):
            return jsonify({'error': 'TIFF file not found'}), 404
    
    tif_meta_start = time.perf_counter()
    tif_sig = get_file_signature(tif_path)
    _, _, _, _ = get_tif_metadata_cached(tif_path, tif_sig)
    log_timing("layout_tif_metadata", tif_meta_start, date=date_str, project=project, tif=os.path.basename(tif_path))
    
    # Get base image dimensions
    base_image_path = os.path.join(date_folder_path, base_image_name)
    downscaled_path = base_image_path.replace('.jpg', '_web.jpg')
    display_image_path = downscaled_path if os.path.exists(downscaled_path) else base_image_path
    base_image_sig = optional_file_signature(base_image_path)
    display_image_sig = optional_file_signature(display_image_path)
    overlay_path = os.path.join(date_folder_path, overlay_image_name) if overlay_image_name else ""
    overlay_sig = optional_file_signature(overlay_path)

    layout_etag = build_etag(
        "layout",
        "tracker_info_v2",
        project,
        date_str,
        tif_sig,
        json_sig,
        csv_sig,
        base_image_sig,
        display_image_sig,
        overlay_sig,
    )
    if request_etag_matches(layout_etag):
        return make_not_modified_response(layout_etag)

    response_data = build_default_layout_response_cached(
        date_str,
        project,
        tif_path,
        tif_sig,
        json_path,
        json_sig,
        csv_path if csv_sig != (0, 0) else "",
        csv_sig,
        base_image_path if base_image_sig != (0, 0) else "",
        base_image_sig,
        display_image_path if display_image_sig != (0, 0) else "",
        display_image_sig,
        base_image_name,
        overlay_image_name or "",
    )
    response = jsonify(response_data)
    apply_cache_headers(response, layout_etag)
    log_timing("get_layout_data", request_start, date=date_str, project=project)
    return response

@app.route('/api/image/layout/<date_str>/<path:filename>')
def get_layout_image(date_str, filename):
    """Serve layout JPG image - creates downscaled version if too large"""
    request_start = time.perf_counter()
    # Find the date folder
    project = get_project_from_request()
    if project_has_zones(project):
        return jsonify({'error': 'Use /api/zone/image endpoint for zone-based projects'}), 400
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
                downscale_start = time.perf_counter()
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
                log_timing("layout_image_downscale", downscale_start, date=date_str, source=filename)
            
            # Serve the downscaled version
            file_path = downscaled_path
            file_size = os.path.getsize(file_path)
            print(f"Serving downscaled version: {file_size} bytes")

        image_sig = get_file_signature(file_path)
        requested_format = (request.args.get("format") or "").lower()
        accepts_webp = "image/webp" in (request.headers.get("Accept") or "").lower()
        target_format = "webp" if (requested_format == "webp" or (requested_format != "jpg" and accepts_webp)) else "jpeg"
        quality = WEBP_QUALITY if target_format == "webp" else JPEG_QUALITY

        etag = build_etag("layout-image", file_path, image_sig, target_format, quality, WEB_IMAGE_MAX_DIMENSION)
        if request_etag_matches(etag):
            return make_not_modified_response(etag)

        image_bytes, content_type, out_w, out_h = encode_image_file_cached(
            file_path,
            image_sig,
            target_format,
            quality,
            WEB_IMAGE_MAX_DIMENSION,
        )

        response = make_response(image_bytes)
        response.headers["Content-Type"] = content_type
        response.headers["Content-Length"] = str(len(image_bytes))
        apply_cache_headers(response, etag)
        log_timing(
            "get_layout_image",
            request_start,
            date=date_str,
            file=os.path.basename(file_path),
            bytes=len(image_bytes),
            width=out_w,
            height=out_h,
            fmt=target_format,
        )
        return response
    except Exception as e:
        log_timing("get_layout_image_failed", request_start, date=date_str, file=filename)
        print(f"Error serving image: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/zones')
@app.route('/api/sonrisa/zones')
def get_zones():
    request_start = time.perf_counter()
    project = get_project_from_request()
    project_layout_dir = get_layout_dir(project)
    date_id = request.args.get("date")
    setup_start = time.perf_counter()
    zone_bounds = _zone_bounds_cached(project)
    available_zones = list(_available_zones_cached(project))
    log_timing(
        "zones_calc_setup",
        setup_start,
        project=project,
        date=date_id,
        zone_bounds=len(zone_bounds),
        available=len(available_zones),
    )

    if not zone_bounds:
        return jsonify({'error': 'Zone boundaries not found'}), 404

    # Batch-fetch all zone stages in ONE directory scan (avoids 54x listdir).
    # Skip entirely when no date is requested — stages are unused for initial load.
    if date_id:
        stage_start = time.perf_counter()
        # Forward-fill: zones without data on date_id use their most recent prior flight.
        zone_stage = _all_zone_stages_forward_fill_cached(project, date_id)
        available_zones = [z for z in available_zones if zone_stage.get(z)]
        log_timing(
            "zones_calc_stage_filter",
            stage_start,
            project=project,
            date=date_id,
            staged=len(zone_stage),
            available=len(available_zones),
        )
    else:
        zone_stage = {}

    overall_start = time.perf_counter()
    overall_bounds = compute_zone_overall_bounds(zone_bounds)
    log_timing("zones_calc_overall_bounds", overall_start, project=project, date=date_id)
    if not overall_bounds:
        return jsonify({'error': 'Unable to compute zone bounds'}), 500

    if date_id:
        if is_blob_video_backend_enabled():
            zone_video_map = discover_zone_videos_blob_cached(project, date_id)
        elif is_blob_video_mount_backend_enabled():
            zone_video_map = discover_zone_videos_blob_mount_cached(project, date_id)
        else:
            _dir_sig = _get_dir_signature(project_layout_dir)
            zone_video_map = discover_zone_videos_cached(project_layout_dir, date_id, _dir_sig)
    else:
        zone_video_map = {}
    serialize_start = time.perf_counter()
    response = jsonify({
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
                'stage': zone_stage.get(zone),
                'video_clips': zone_video_map.get(zone, []),
                'has_video': bool(zone_video_map.get(zone)),
            }
            for zone, bounds in sorted(zone_bounds.items())
        ],
        'available_zones': available_zones,
        'overall_bounds': overall_bounds,
        'block_map_url': '/api/block_map'
    })
    log_timing("zones_calc_response_serialize", serialize_start, project=project, date=date_id)
    log_timing("get_zones", request_start, project=project, date=date_id, zones=len(available_zones))
    return response


@app.route('/api/block_map')
@app.route('/api/sonrisa/block_map')
def get_block_map():
    request_start = time.perf_counter()
    project = get_project_from_request()
    project_layout_dir = get_layout_dir(project)
    date_id = request.args.get("date")
    bg_candidates = [
        os.path.join(project_layout_dir, f"{project.lower()}_block_map.jpg"),
        os.path.join(project_layout_dir, "sonrisa_block_map.jpg"),
        os.path.join(project_layout_dir, "block_map.jpg"),
    ]
    existing_map = next((p for p in bg_candidates if os.path.exists(p)), None)
    zone_bounds = _zone_bounds_cached(project)
    available_zones = list(_available_zones_cached(project))
    zone_colors = {}
    if date_id:
        zone_stage = _all_zone_stages_cached(project, date_id)
        for zone, stage in zone_stage.items():
            color = STAGE_COLORS.get(stage)
            if color:
                zone_colors[zone] = color
        available_zones = [z for z in available_zones if z in zone_colors]
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
    log_timing("get_block_map", request_start, project=project, date=date_id, zones=len(available_zones))
    return response


@app.route('/api/block_map_bg')
def get_block_map_bg():
    """Serve the static background block-map image (no zone overlays).
    Works for any zone-based project. The frontend draws overlays on a canvas."""
    request_start = time.perf_counter()
    project = get_project_from_request()
    project_layout_dir = get_layout_dir(project)
    bg_candidates = [
        os.path.join(project_layout_dir, f"{project.lower()}_block_map.jpg"),
        os.path.join(project_layout_dir, "sonrisa_block_map.jpg"),
        os.path.join(project_layout_dir, "block_map.jpg"),
    ]
    existing_map = next((p for p in bg_candidates if os.path.exists(p)), None)
    if existing_map:
        sig = get_file_signature(existing_map)
        etag = build_etag("block_map_bg", project, sig)
        if request_etag_matches(etag):
            log_timing("get_block_map_bg", request_start, project=project, result="304")
            return make_not_modified_response(etag)
        fmt = "webp" if "image/webp" in request.headers.get("Accept", "") else "jpeg"
        img_bytes, content_type, _, _ = encode_image_file_cached(
            existing_map,
            sig,
            fmt,
            85 if fmt == "webp" else 90,
            1600,
        )
        response = make_response(img_bytes)
        response.headers["Content-Type"] = content_type
        response.headers["Content-Length"] = str(len(img_bytes))
        apply_cache_headers(response, etag, max_age=0)
    else:
        zone_bounds = get_sonrisa_zone_bounds(project)
        if not zone_bounds:
            return jsonify({'error': 'No zone data for this project'}), 404
        overall = compute_zone_overall_bounds(zone_bounds)
        if not overall:
            return jsonify({'error': 'Unable to compute bounds'}), 500
        img = Image.new("RGBA", (1600, 900), (245, 245, 245, 255))
        buffer = BytesIO()
        img.save(buffer, format='PNG')
        buffer.seek(0)
        response = send_file(buffer, mimetype='image/png')
        response.headers['Cache-Control'] = 'no-cache'
    log_timing("get_block_map_bg", request_start, project=project)
    return response


@app.route('/api/site_date_summary/<date_str>')
def get_site_date_summary(date_str):
    """Return site-level summary aggregated across all zones for a date.
    Returns the same shape as /api/date_summary so the frontend charts/tables
    work without requiring a zone selection."""
    request_start = time.perf_counter()
    # Normalise date to YYYYMMDD so string comparisons against folder names work
    # regardless of whether the caller passes YYYY-MM-DD or YYYYMMDD.
    date_str = date_str.replace('-', '')
    project = get_project_from_request()
    project_layout_dir = get_layout_dir(project)
    available_zones = list(_available_zones_cached(project))

    agg_tracker_stages = []
    agg_stage_status_counts = {}
    agg_stage_progress_status_counts = _init_stage_status_matrix()
    total_trackers = 0

    for zone in available_zones:
        best = _zone_most_recent_folder_at_or_before(project, zone, date_str)
        if not best:
            continue
        folder_name = best[1]
        tracker_info = get_tracker_info_from_sources(
            project, zone, None, (0, 0), None, (0, 0), folder_name_or_latest=folder_name
        )
        if not tracker_info:
            continue
        total_trackers += len(tracker_info)
        for info in tracker_info.values():
            stage_key = (info.get("stage") or "").lower().replace(" ", "_")
            status_key = (info.get("status") or "").lower().replace(" ", "_")
            if stage_key:
                agg_tracker_stages.append({"stage": stage_key, "status": status_key})
            if stage_key not in agg_stage_status_counts:
                agg_stage_status_counts[stage_key] = {}
            agg_stage_status_counts[stage_key][status_key] = (
                agg_stage_status_counts[stage_key].get(status_key, 0) + 1
            )
            _accumulate_normalized_stage_progress(
                agg_stage_progress_status_counts,
                stage_key,
                status_key,
            )

    summary = {
        "stageStatusCounts": agg_stage_status_counts,
        "stageProgressStatusCounts": agg_stage_progress_status_counts,
        "totalTrackers": total_trackers,
        "trackerStages": agg_tracker_stages,
    }
    log_timing("get_site_date_summary", request_start, project=project, date=date_str, zones=len(available_zones))
    return jsonify(summary)


@app.route('/api/multi_date_summary')
def get_multi_date_summary():
    """Return site-level summaries for ALL available flight dates in one payload.
    Used by trend / rate / productivity charts that need cross-date data."""
    request_start = time.perf_counter()
    project = get_project_from_request()
    project_layout_dir = get_layout_dir(project)
    all_dates = list(_all_dates_cached(project))
    available_zones = list(_available_zones_cached(project))

    summaries = {}
    for date_obj in all_dates:
        date_id = date_obj["date"]
        zone_stage = _all_zone_stages_forward_fill_cached(project, date_id)
        active_zones = [z for z in available_zones if zone_stage.get(z)]

        agg_stage_status = {}
        agg_stage_progress = _init_stage_status_matrix()
        zone_rows = []
        total_trackers = 0

        for zone in active_zones:
            best = _zone_most_recent_folder_at_or_before(project, zone, date_id)
            if not best:
                continue
            folder_name = best[1]
            tracker_info = get_tracker_info_from_sources(
                project, zone, folder_name_or_latest=folder_name
            )
            if not tracker_info:
                continue

            total = len(tracker_info)
            total_trackers += total
            completed = 0
            for info in tracker_info.values():
                stage = (info.get("stage") or "").lower().replace(" ", "_")
                status = (info.get("status") or "").lower().replace(" ", "_")
                status_norm = (status or "not_started").strip().lower().replace(" ", "_") or "not_started"
                if stage == "solar_panel" and status_norm == "completed":
                    completed += 1
                if not stage:
                    continue
                if stage not in agg_stage_status:
                    agg_stage_status[stage] = {}
                agg_stage_status[stage][status_norm] = agg_stage_status[stage].get(status_norm, 0) + 1
                _accumulate_normalized_stage_progress(agg_stage_progress, stage, status_norm)

            pct = (completed / total * 100) if total > 0 else 0.0
            zone_rows.append({"zone": zone, "total": total, "completed": completed, "pct": f"{pct:.1f}"})

        summaries[date_id] = {
            "stageStatusCounts": agg_stage_status,
            "stageProgressStatusCounts": agg_stage_progress,
            "totalTrackers": total_trackers,
            "zone_summaries": zone_rows,
        }

    manpower = build_manpower_data_response(project)
    project_settings = build_project_settings_response(project)

    response = jsonify({
        "dates": [d["date"] for d in all_dates],
        "date_displays": {d["date"]: d["display"] for d in all_dates},
        "summaries": summaries,
        "manpower": manpower,
        "project_settings": project_settings,
    })
    log_timing("get_multi_date_summary", request_start, project=project, dates=len(all_dates))
    return response


@app.route('/api/clear_cache', methods=['POST'])
def clear_cache():
    """Clear all LRU caches so fresh data is served immediately after data migrations."""
    _clear_all_caches()
    return jsonify({'status': 'ok', 'message': 'All caches cleared'})


@app.route('/api/ingest', methods=['POST'])
def trigger_ingest():
    """Manually trigger a full re-scan of layout_data and push to PostgreSQL.

    Accepts an optional JSON body:
      { "project": "Sonrisa", "folder": "G1_2_8_9_10_74m_70_80_ovrlp_20260212" }

    When 'folder' is omitted, all flight folders under the project are scanned.
    When 'project' is also omitted, all projects in layout_data are scanned.
    """
    body = request.get_json(silent=True) or {}
    project_filter = body.get("project")
    folder_filter = body.get("folder")

    results = {}
    try:
        projects_to_scan = []
        if project_filter:
            project_dir = os.path.join(BASE_LAYOUT_DIR, project_filter)
            if os.path.isdir(project_dir):
                projects_to_scan = [(project_filter, project_dir)]
        else:
            for entry in sorted(os.listdir(BASE_LAYOUT_DIR)):
                entry_path = os.path.join(BASE_LAYOUT_DIR, entry)
                if os.path.isdir(entry_path) and not entry.startswith("_"):
                    projects_to_scan.append((entry, entry_path))

        for project_name, project_dir in projects_to_scan:
            if folder_filter:
                folder_path = os.path.join(project_dir, folder_filter)
                flight_date, _ = parse_folder(folder_filter)
                if not flight_date:
                    results[f"{project_name}/{folder_filter}"] = {"error": "folder name has no date"}
                    continue
                try:
                    result = ingest_flight_folder(project_name, folder_filter, folder_path)
                    results[f"{project_name}/{folder_filter}"] = result
                except Exception as exc:
                    results[f"{project_name}/{folder_filter}"] = {"error": str(exc)}
            else:
                try:
                    result = ingest_project(project_name, project_dir)
                    results[project_name] = result
                except Exception as exc:
                    results[project_name] = {"error": str(exc)}

        _clear_all_caches()
        return jsonify({'status': 'ok', 'results': results})

    except Exception as exc:
        return jsonify({'status': 'error', 'message': str(exc)}), 500


@app.route('/api/zone_dates')
@app.route('/api/sonrisa/dates')
def get_zone_dates():
    request_start = time.perf_counter()
    project = get_project_from_request()
    project_layout_dir = get_layout_dir(project)
    dates = list(_all_dates_cached(project))
    response = jsonify({'dates': dates})
    log_timing("get_zone_dates", request_start, project=project, count=len(dates))
    return response


@app.route('/api/site_overview')
@app.route('/api/sonrisa/site_overview')
def get_site_overview():
    """Return site-level zone summary (total, completed, %) for a date. CSV-only, no TIFFs."""
    request_start = time.perf_counter()
    project = get_project_from_request()
    date_id = request.args.get("date")
    if not date_id:
        return jsonify({'error': 'Date required'}), 400

    project_layout_dir = get_layout_dir(project)
    setup_start = time.perf_counter()
    available_zones = list(_available_zones_cached(project))
    # Forward-fill: include any zone that has data at or before date_id.
    zone_stage = _all_zone_stages_forward_fill_cached(project, date_id)
    available_zones = [z for z in available_zones if zone_stage.get(z)]
    log_timing(
        "site_overview_calc_setup",
        setup_start,
        project=project,
        date=date_id,
        available=len(available_zones),
        staged=len(zone_stage),
    )

    rows = []
    stage_counts = {}
    stage_status_counts = {}
    stage_progress_status_counts = _init_stage_status_matrix()
    aggregation_start = time.perf_counter()
    for zone in available_zones:
        # Use most-recent flight at or before date_id so forward-filled zones are included.
        best = _zone_most_recent_folder_at_or_before(project, zone, date_id)
        if not best:
            continue
        folder_name = best[1]
        tracker_info = get_tracker_info_from_sources(
            project, zone, folder_name_or_latest=folder_name
        )
        if not tracker_info:
            continue
        total = len(tracker_info)
        completed = 0
        for info in tracker_info.values():
            stage = (info.get("stage") or "").lower().replace(" ", "_")
            status = (info.get("status") or "").lower().replace(" ", "_")
            if stage == "solar_panel" and status == "completed":
                completed += 1
            if stage:
                stage_counts[stage] = stage_counts.get(stage, 0) + 1
                if stage not in stage_status_counts:
                    stage_status_counts[stage] = {}
                status_norm = (status or "not_started").strip().lower().replace(" ", "_") or "not_started"
                stage_status_counts[stage][status_norm] = stage_status_counts[stage].get(status_norm, 0) + 1
                _accumulate_normalized_stage_progress(
                    stage_progress_status_counts,
                    stage,
                    status_norm,
                )
        pct = (completed / total * 100) if total > 0 else 0.0
        rows.append({"zone": zone, "total": total, "completed": completed, "pct": f"{pct:.1f}"})
    log_timing(
        "site_overview_calc_aggregate",
        aggregation_start,
        project=project,
        date=date_id,
        rows=len(rows),
        stages=len(stage_counts),
    )

    completed_zones = sum(1 for r in rows if r["pct"] == "100.0")
    bounds_start = time.perf_counter()
    all_zone_bounds = _zone_bounds_cached(project)
    log_timing("site_overview_calc_bounds", bounds_start, project=project, date=date_id, total_zones=len(all_zone_bounds))
    serialize_start = time.perf_counter()
    response = jsonify({
        "total_zones": len(all_zone_bounds) if all_zone_bounds else len(rows),
        "available_count": len(rows),
        "completed_zones_count": completed_zones,
        "zones": rows,
        "stage_counts": stage_counts,
        "stage_status_counts": stage_status_counts,
        "stage_progress_status_counts": stage_progress_status_counts,
    })
    log_timing("site_overview_calc_response_serialize", serialize_start, project=project, date=date_id)
    log_timing("get_site_overview", request_start, project=project, date=date_id, rows=len(rows))
    return response


@app.route('/api/date_bundle')
def get_date_bundle():
    """Return date-scoped zones + site-overview + summary in one payload."""
    request_start = time.perf_counter()
    project = get_project_from_request()
    date_id = request.args.get("date")
    if not date_id:
        return jsonify({'error': 'Date required'}), 400

    project_layout_dir = get_layout_dir(project)
    zone_bounds = _zone_bounds_cached(project)
    available_zones = list(_available_zones_cached(project))
    zone_stage = _all_zone_stages_forward_fill_cached(project, date_id)
    available_zones = [z for z in available_zones if zone_stage.get(z)]
    overall_bounds = compute_zone_overall_bounds(zone_bounds)
    if not overall_bounds:
        return jsonify({'error': 'Unable to compute zone bounds'}), 500

    dir_sig = _get_dir_signature(project_layout_dir)
    include_videos = (request.args.get("include_videos", "0").strip().lower() in ("1", "true", "yes", "on"))
    if include_videos:
        if is_blob_video_backend_enabled():
            zone_video_map = discover_zone_videos_blob_cached(project, date_id)
        elif is_blob_video_mount_backend_enabled():
            zone_video_map = discover_zone_videos_blob_mount_cached(project, date_id)
        else:
            zone_video_map = discover_zone_videos_cached(project_layout_dir, date_id, dir_sig)
    else:
        zone_video_map = {}

    rows = []
    stage_counts = {}
    stage_status_counts = {}
    stage_progress_status_counts = _init_stage_status_matrix()
    agg_tracker_stages = []
    agg_stage_status_counts = {}
    agg_stage_progress_status_counts = _init_stage_status_matrix()
    total_trackers = 0

    for zone in available_zones:
        best = _zone_most_recent_folder_at_or_before(project, zone, date_id)
        if not best:
            continue
        folder_name = best[1]
        tracker_info = get_tracker_info_from_sources(
            project, zone, folder_name_or_latest=folder_name
        )
        if not tracker_info:
            continue

        total = len(tracker_info)
        total_trackers += total
        completed = 0
        for info in tracker_info.values():
            stage = (info.get("stage") or "").lower().replace(" ", "_")
            status = (info.get("status") or "").lower().replace(" ", "_")
            status_norm = (status or "not_started").strip().lower().replace(" ", "_") or "not_started"
            if stage == "solar_panel" and status_norm == "completed":
                completed += 1
            if not stage:
                continue

            stage_counts[stage] = stage_counts.get(stage, 0) + 1
            if stage not in stage_status_counts:
                stage_status_counts[stage] = {}
            stage_status_counts[stage][status_norm] = stage_status_counts[stage].get(status_norm, 0) + 1
            _accumulate_normalized_stage_progress(
                stage_progress_status_counts,
                stage,
                status_norm,
            )

            agg_tracker_stages.append({"stage": stage, "status": status_norm})
            if stage not in agg_stage_status_counts:
                agg_stage_status_counts[stage] = {}
            agg_stage_status_counts[stage][status_norm] = (
                agg_stage_status_counts[stage].get(status_norm, 0) + 1
            )
            _accumulate_normalized_stage_progress(
                agg_stage_progress_status_counts,
                stage,
                status_norm,
            )

        pct = (completed / total * 100) if total > 0 else 0.0
        rows.append({"zone": zone, "total": total, "completed": completed, "pct": f"{pct:.1f}"})

    completed_zones = sum(1 for r in rows if r["pct"] == "100.0")
    total_zones = len(zone_bounds) if zone_bounds else len(rows)
    bundle = {
        "date": date_id,
        "zones_data": {
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
                    'stage': zone_stage.get(zone),
                    'video_clips': zone_video_map.get(zone, []),
                    'has_video': bool(zone_video_map.get(zone)),
                }
                for zone, bounds in sorted(zone_bounds.items())
            ],
            'available_zones': available_zones,
            'overall_bounds': overall_bounds,
            'block_map_url': '/api/block_map',
        },
        "site_overview": {
            "total_zones": total_zones,
            "available_count": len(rows),
            "completed_zones_count": completed_zones,
            "zones": rows,
            "stage_counts": stage_counts,
            "stage_status_counts": stage_status_counts,
            "stage_progress_status_counts": stage_progress_status_counts,
        },
        "summary": {
            "stageStatusCounts": agg_stage_status_counts,
            "stageProgressStatusCounts": agg_stage_progress_status_counts,
            "totalTrackers": total_trackers,
            "trackerStages": agg_tracker_stages,
        },
    }
    log_timing(
        "get_date_bundle",
        request_start,
        project=project,
        date=date_id,
        zones=len(available_zones),
        rows=len(rows),
    )
    return jsonify(bundle)


@app.route('/api/zone/image/<zone>/<date_str>')
@app.route('/api/sonrisa/image/<zone>/<date_str>')
def get_zone_image(zone, date_str):
    request_start = time.perf_counter()
    project = get_project_from_request()
    project_layout_dir = get_layout_dir(project)
    zone = normalize_zone_code(zone)
    if not zone:
        return jsonify({'error': 'Invalid zone'}), 400
    date_folder_path = find_sonrisa_date_folder(project_layout_dir, zone, date_str)
    if not date_folder_path:
        # When a zone has no imagery for the selected date, fallback to the
        # most recent available zone image instead of returning a hard 404.
        fallback_tif_path, fallback_zone_tif, fallback_web_path = find_sonrisa_zone_tif_fallback(project_layout_dir, zone)
        if fallback_tif_path and fallback_zone_tif:
            date_folder_path = os.path.dirname(fallback_tif_path)
            zone_tif = fallback_zone_tif
        else:
            return jsonify({'error': f'Date folder not found for {zone} {date_str}'}), 404
    else:
        zone_tif = None

    tif_candidates = [f for f in os.listdir(date_folder_path) if f.lower().endswith('.tif')]
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
        fallback_tif_path, fallback_zone_tif, fallback_web_path = find_sonrisa_zone_tif_fallback(project_layout_dir, zone)
        if fallback_tif_path and fallback_zone_tif:
            date_folder_path = os.path.dirname(fallback_tif_path)
            zone_tif = fallback_zone_tif
        else:
            # New folder format: no TIF available; serve the pre-rendered JPG directly.
            prerendered = find_prerendered_zone_jpg(date_folder_path, zone)
            if not prerendered:
                return jsonify({'error': 'Zone TIFF not found'}), 404
            web_path = prerendered
            web_sig = get_file_signature(web_path)
            requested_format = (request.args.get("format") or "").lower()
            accepts_webp = "image/webp" in (request.headers.get("Accept") or "").lower()
            target_format = "webp" if (requested_format == "webp" or (requested_format != "jpg" and accepts_webp)) else "jpeg"
            quality = WEBP_QUALITY if target_format == "webp" else JPEG_QUALITY
            etag = build_etag("sonrisa-image", web_path, web_sig, target_format, quality, WEB_IMAGE_MAX_DIMENSION)
            if request_etag_matches(etag):
                return make_not_modified_response(etag)
            image_bytes, content_type, out_w, out_h = encode_image_file_cached(
                web_path, web_sig, target_format, quality, WEB_IMAGE_MAX_DIMENSION,
            )
            response = make_response(image_bytes)
            response.headers["Content-Type"] = content_type
            response.headers["Content-Length"] = str(len(image_bytes))
            apply_cache_headers(response, etag)
            log_timing("get_sonrisa_image_cached", request_start, zone=zone, date=date_str,
                       file=os.path.basename(web_path), bytes=len(image_bytes),
                       width=out_w, height=out_h, fmt=target_format)
            return response
    web_path = get_or_create_sonrisa_web_jpg(date_folder_path, zone_tif, max_dimension=4000)
    if web_path and os.path.exists(web_path):
        web_sig = get_file_signature(web_path)
        requested_format = (request.args.get("format") or "").lower()
        accepts_webp = "image/webp" in (request.headers.get("Accept") or "").lower()
        target_format = "webp" if (requested_format == "webp" or (requested_format != "jpg" and accepts_webp)) else "jpeg"
        quality = WEBP_QUALITY if target_format == "webp" else JPEG_QUALITY

        etag = build_etag("sonrisa-image", web_path, web_sig, target_format, quality, WEB_IMAGE_MAX_DIMENSION)
        if request_etag_matches(etag):
            return make_not_modified_response(etag)

        image_bytes, content_type, out_w, out_h = encode_image_file_cached(
            web_path,
            web_sig,
            target_format,
            quality,
            WEB_IMAGE_MAX_DIMENSION,
        )
        response = make_response(image_bytes)
        response.headers["Content-Type"] = content_type
        response.headers["Content-Length"] = str(len(image_bytes))
        apply_cache_headers(response, etag)
        log_timing(
            "get_sonrisa_image_cached",
            request_start,
            zone=zone,
            date=date_str,
            file=os.path.basename(web_path),
            bytes=len(image_bytes),
            width=out_w,
            height=out_h,
            fmt=target_format,
        )
        return response

    tif_path = os.path.join(date_folder_path, zone_tif)

    try:
        convert_start = time.perf_counter()
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
        log_timing("sonrisa_tif_to_jpeg", convert_start, zone=zone, date=date_str, tif=zone_tif, width=img.width, height=img.height)
        log_timing("get_sonrisa_image", request_start, zone=zone, date=date_str, tif=zone_tif)
        return send_file(buffer, mimetype='image/jpeg')
    except Exception as e:
        log_timing("get_sonrisa_image_failed", request_start, zone=zone, date=date_str, tif=zone_tif)
        return jsonify({'error': str(e)}), 500


@app.route('/api/zone/video/<zone>/<date_str>/<path:clip_name>')
@app.route('/api/sonrisa/video/<zone>/<date_str>/<path:clip_name>')
def get_zone_video(zone, date_str, clip_name):
    request_start = time.perf_counter()
    project = get_project_from_request()
    project_layout_dir = get_layout_dir(project)
    zone = normalize_zone_code(zone)
    if not zone:
        return jsonify({'error': 'Invalid zone'}), 400
    if not clip_name:
        return jsonify({'error': 'Invalid clip path'}), 400

    if is_blob_video_backend_enabled():
        normalized_rel = normalize_blob_path(clip_name)
        if not normalized_rel:
            return jsonify({'error': 'Invalid clip path'}), 400
        _, ext = os.path.splitext(normalized_rel)
        if ext.lower() not in ZONE_VIDEO_EXTENSIONS:
            return jsonify({'error': 'Unsupported video format'}), 400
        if not _blob_path_matches_project(project, normalized_rel):
            return jsonify({'error': 'Video clip outside project scope'}), 403
        if not _is_blob_video_candidate_path(normalized_rel):
            return jsonify({'error': 'Video clip outside videos path'}), 403
        if not _video_rel_matches_date(normalized_rel, date_str):
            return jsonify({'error': 'Video clip does not match requested date'}), 404
        if not _zone_matches_video_path(zone, normalized_rel):
            return jsonify({'error': 'Video clip does not match requested zone'}), 404
        if not blob_exists(normalized_rel):
            return jsonify({'error': 'Video clip not found'}), 404
        sas_url = build_blob_video_sas_url(normalized_rel)
        if not sas_url:
            return jsonify({'error': 'Blob video backend is not configured'}), 500
        log_timing("get_zone_video", request_start, project=project, zone=zone, date=date_str, clip=normalized_rel, result="302_blob_sas")
        return redirect(sas_url, code=302)

    if is_blob_video_mount_backend_enabled():
        if not _blob_mount_ready():
            return jsonify({'error': 'Blob mount path is not configured or accessible'}), 500
        normalized_rel = normalize_blob_path(clip_name)
        if not normalized_rel:
            return jsonify({'error': 'Invalid clip path'}), 400
        _, ext = os.path.splitext(normalized_rel)
        if ext.lower() not in ZONE_VIDEO_EXTENSIONS:
            return jsonify({'error': 'Unsupported video format'}), 400
        if not _blob_mount_path_matches_project(project, normalized_rel):
            return jsonify({'error': 'Video clip outside project scope'}), 403
        if not _is_blob_video_candidate_path(normalized_rel):
            return jsonify({'error': 'Video clip outside videos path'}), 403
        if not _video_rel_matches_date(normalized_rel, date_str):
            return jsonify({'error': 'Video clip does not match requested date'}), 404
        if not _zone_matches_video_path(zone, normalized_rel):
            return jsonify({'error': 'Video clip does not match requested zone'}), 404
        clip_path, _ = _resolve_blob_mount_path(normalized_rel)
        if not clip_path or not os.path.exists(clip_path) or not os.path.isfile(clip_path):
            return jsonify({'error': 'Video clip not found'}), 404

        serve_path = get_or_create_browser_compatible_video(clip_path)
        clip_sig = get_file_signature(serve_path)
        etag = build_etag("zone-video-mount", project, zone, date_str, normalized_rel, clip_sig)
        if request_etag_matches(etag):
            log_timing("get_zone_video", request_start, project=project, zone=zone, date=date_str, clip=normalized_rel, result="304")
            return make_not_modified_response(etag)
        mimetype, _ = mimetypes.guess_type(serve_path)
        response = send_file(serve_path, mimetype=(mimetype or "video/mp4"), conditional=True)
        apply_cache_headers(response, etag)
        log_timing("get_zone_video", request_start, project=project, zone=zone, date=date_str, clip=normalized_rel, result="blob_mount")
        return response

    video_roots = resolve_zone_video_roots(project_layout_dir, date_str)
    if not video_roots:
        return jsonify({'error': 'No zone videos for this date'}), 404
    clip_path, normalized_rel = _resolve_zone_video_path(project_layout_dir, clip_name)
    if not clip_path:
        return jsonify({'error': 'Invalid clip path'}), 400
    if not os.path.exists(clip_path) or not os.path.isfile(clip_path):
        return jsonify({'error': 'Video clip not found'}), 404
    _, ext = os.path.splitext(clip_path)
    if ext.lower() not in ZONE_VIDEO_EXTENSIONS:
        return jsonify({'error': 'Unsupported video format'}), 400
    if not _video_rel_matches_date(normalized_rel, date_str):
        return jsonify({'error': 'Video clip does not match requested date'}), 404
    if not _zone_matches_video_path(zone, normalized_rel):
        return jsonify({'error': 'Video clip does not match requested zone'}), 404

    serve_path = get_or_create_browser_compatible_video(clip_path)
    clip_sig = get_file_signature(serve_path)
    etag = build_etag("zone-video", project, zone, date_str, normalized_rel, clip_sig)
    if request_etag_matches(etag):
        log_timing("get_zone_video", request_start, project=project, zone=zone, date=date_str, clip=normalized_rel, result="304")
        return make_not_modified_response(etag)

    mimetype, _ = mimetypes.guess_type(serve_path)
    response = send_file(serve_path, mimetype=(mimetype or "video/mp4"), conditional=True)
    apply_cache_headers(response, etag)
    log_timing("get_zone_video", request_start, project=project, zone=zone, date=date_str, clip=normalized_rel)
    return response


@app.route('/api/zone/video/hls/<zone>/<date_str>/<path:manifest_path>')
@app.route('/api/sonrisa/video/hls/<zone>/<date_str>/<path:manifest_path>')
def get_zone_video_hls_manifest(zone, date_str, manifest_path):
    request_start = time.perf_counter()
    project = get_project_from_request()
    project_layout_dir = get_layout_dir(project)
    zone = normalize_zone_code(zone)
    if not zone:
        return jsonify({'error': 'Invalid zone'}), 400
    if is_blob_video_backend_enabled():
        normalized_manifest = normalize_blob_path(manifest_path)
        if not normalized_manifest:
            return jsonify({'error': 'Invalid manifest path'}), 400
        if os.path.splitext(normalized_manifest)[1].lower() not in HLS_MANIFEST_EXTENSIONS:
            return jsonify({'error': 'Unsupported manifest format'}), 400
        if not _blob_path_matches_project(project, normalized_manifest):
            return jsonify({'error': 'Manifest outside project scope'}), 403
        if not _is_blob_video_candidate_path(normalized_manifest):
            return jsonify({'error': 'Manifest outside videos path'}), 403
        if not _video_rel_matches_date(normalized_manifest, date_str):
            return jsonify({'error': 'Manifest does not match requested date'}), 404
        if not _zone_matches_video_path(zone, normalized_manifest):
            return jsonify({'error': 'Manifest does not match requested zone'}), 404
        manifest_text = read_blob_text(normalized_manifest)
        if manifest_text is None:
            return jsonify({'error': 'Manifest not found'}), 404
        etag = build_etag("zone-hls-manifest-blob", project, zone, date_str, normalized_manifest, len(manifest_text))
        if request_etag_matches(etag):
            return make_not_modified_response(etag)
        rewritten_manifest = _rewrite_hls_manifest(manifest_text, zone, date_str, normalized_manifest)
        response = make_response(rewritten_manifest)
        response.headers["Content-Type"] = "application/vnd.apple.mpegurl"
        apply_cache_headers(response, etag)
        log_timing("get_zone_video_hls_manifest", request_start, project=project, zone=zone, date=date_str, manifest=normalized_manifest, result="blob")
        return response

    if is_blob_video_mount_backend_enabled():
        if not _blob_mount_ready():
            return jsonify({'error': 'Blob mount path is not configured or accessible'}), 500
        normalized_manifest = normalize_blob_path(manifest_path)
        if not normalized_manifest:
            return jsonify({'error': 'Invalid manifest path'}), 400
        if os.path.splitext(normalized_manifest)[1].lower() not in HLS_MANIFEST_EXTENSIONS:
            return jsonify({'error': 'Unsupported manifest format'}), 400
        if not _blob_mount_path_matches_project(project, normalized_manifest):
            return jsonify({'error': 'Manifest outside project scope'}), 403
        if not _is_blob_video_candidate_path(normalized_manifest):
            return jsonify({'error': 'Manifest outside videos path'}), 403
        if not _video_rel_matches_date(normalized_manifest, date_str):
            return jsonify({'error': 'Manifest does not match requested date'}), 404
        if not _zone_matches_video_path(zone, normalized_manifest):
            return jsonify({'error': 'Manifest does not match requested zone'}), 404
        manifest_abs_path, _ = _resolve_blob_mount_path(normalized_manifest)
        if not manifest_abs_path or not os.path.exists(manifest_abs_path) or not os.path.isfile(manifest_abs_path):
            return jsonify({'error': 'Manifest not found'}), 404
        manifest_sig = get_file_signature(manifest_abs_path)
        etag = build_etag("zone-hls-manifest-mount", project, zone, date_str, normalized_manifest, manifest_sig)
        if request_etag_matches(etag):
            return make_not_modified_response(etag)
        with open(manifest_abs_path, "r", encoding="utf-8", errors="ignore") as handle:
            manifest_text = handle.read()
        rewritten_manifest = _rewrite_hls_manifest(manifest_text, zone, date_str, normalized_manifest)
        response = make_response(rewritten_manifest)
        response.headers["Content-Type"] = "application/vnd.apple.mpegurl"
        apply_cache_headers(response, etag)
        log_timing("get_zone_video_hls_manifest", request_start, project=project, zone=zone, date=date_str, manifest=normalized_manifest, result="blob_mount")
        return response

    video_roots = resolve_zone_video_roots(project_layout_dir, date_str)
    if not video_roots:
        return jsonify({'error': 'No zone videos for this date'}), 404
    manifest_abs_path, normalized_manifest = _resolve_zone_video_path(project_layout_dir, manifest_path)
    if not manifest_abs_path:
        return jsonify({'error': 'Invalid manifest path'}), 400
    if not os.path.exists(manifest_abs_path) or not os.path.isfile(manifest_abs_path):
        return jsonify({'error': 'Manifest not found'}), 404
    if os.path.splitext(manifest_abs_path)[1].lower() not in HLS_MANIFEST_EXTENSIONS:
        return jsonify({'error': 'Unsupported manifest format'}), 400
    if not _video_rel_matches_date(normalized_manifest, date_str):
        return jsonify({'error': 'Manifest does not match requested date'}), 404
    if not _zone_matches_video_path(zone, normalized_manifest):
        return jsonify({'error': 'Manifest does not match requested zone'}), 404

    manifest_sig = get_file_signature(manifest_abs_path)
    etag = build_etag("zone-hls-manifest", project, zone, date_str, normalized_manifest, manifest_sig)
    if request_etag_matches(etag):
        return make_not_modified_response(etag)

    with open(manifest_abs_path, "r", encoding="utf-8", errors="ignore") as handle:
        manifest_text = handle.read()
    rewritten_manifest = _rewrite_hls_manifest(manifest_text, zone, date_str, normalized_manifest)
    response = make_response(rewritten_manifest)
    response.headers["Content-Type"] = "application/vnd.apple.mpegurl"
    apply_cache_headers(response, etag)
    log_timing("get_zone_video_hls_manifest", request_start, project=project, zone=zone, date=date_str, manifest=normalized_manifest)
    return response


@app.route('/api/zone/video/hls_asset/<zone>/<date_str>/<path:manifest_path>')
@app.route('/api/sonrisa/video/hls_asset/<zone>/<date_str>/<path:manifest_path>')
def get_zone_video_hls_asset(zone, date_str, manifest_path, asset_path=None):
    request_start = time.perf_counter()
    project = get_project_from_request()
    project_layout_dir = get_layout_dir(project)
    zone = normalize_zone_code(zone)
    if not zone:
        return jsonify({'error': 'Invalid zone'}), 400
    if asset_path is None:
        asset_path = request.args.get("asset")
    if not asset_path:
        return jsonify({'error': 'Invalid asset path'}), 400
    if is_blob_video_backend_enabled():
        normalized_manifest = normalize_blob_path(manifest_path)
        if not normalized_manifest:
            return jsonify({'error': 'Invalid manifest path'}), 400
        if os.path.splitext(normalized_manifest)[1].lower() not in HLS_MANIFEST_EXTENSIONS:
            return jsonify({'error': 'Unsupported manifest format'}), 400
        if not _blob_path_matches_project(project, normalized_manifest):
            return jsonify({'error': 'Manifest outside project scope'}), 403
        if not _is_blob_video_candidate_path(normalized_manifest):
            return jsonify({'error': 'Manifest outside videos path'}), 403
        if not _video_rel_matches_date(normalized_manifest, date_str):
            return jsonify({'error': 'Manifest does not match requested date'}), 404
        if not _zone_matches_video_path(zone, normalized_manifest):
            return jsonify({'error': 'Manifest does not match requested zone'}), 404

        manifest_dir = os.path.dirname(normalized_manifest)
        asset_input = normalize_blob_path(asset_path)
        if not asset_input:
            return jsonify({'error': 'Invalid asset path'}), 400
        if manifest_dir and (asset_input == manifest_dir or asset_input.startswith(f"{manifest_dir}/")):
            joined_asset_rel = asset_input
        else:
            joined_asset_rel = normalize_blob_path(os.path.join(manifest_dir, asset_input).replace("\\", "/"))
        if not joined_asset_rel:
            return jsonify({'error': 'Invalid asset path'}), 400
        if manifest_dir and not joined_asset_rel.startswith(f"{manifest_dir}/") and joined_asset_rel != manifest_dir:
            return jsonify({'error': 'Asset path outside manifest directory'}), 400
        asset_ext = os.path.splitext(joined_asset_rel)[1].lower()
        if asset_ext not in HLS_ASSET_EXTENSIONS:
            return jsonify({'error': 'Unsupported HLS asset format'}), 400
        if not _blob_path_matches_project(project, joined_asset_rel):
            return jsonify({'error': 'Asset outside project scope'}), 403
        if not _is_blob_video_candidate_path(joined_asset_rel):
            return jsonify({'error': 'Asset outside videos path'}), 403
        if not blob_exists(joined_asset_rel):
            return jsonify({'error': 'Asset not found'}), 404
        sas_url = build_blob_video_sas_url(joined_asset_rel)
        if not sas_url:
            return jsonify({'error': 'Blob video backend is not configured'}), 500
        log_timing("get_zone_video_hls_asset", request_start, project=project, zone=zone, date=date_str, asset=joined_asset_rel, result="302_blob_sas")
        return redirect(sas_url, code=302)

    if is_blob_video_mount_backend_enabled():
        if not _blob_mount_ready():
            return jsonify({'error': 'Blob mount path is not configured or accessible'}), 500
        normalized_manifest = normalize_blob_path(manifest_path)
        if not normalized_manifest:
            return jsonify({'error': 'Invalid manifest path'}), 400
        if os.path.splitext(normalized_manifest)[1].lower() not in HLS_MANIFEST_EXTENSIONS:
            return jsonify({'error': 'Unsupported manifest format'}), 400
        if not _blob_mount_path_matches_project(project, normalized_manifest):
            return jsonify({'error': 'Manifest outside project scope'}), 403
        if not _is_blob_video_candidate_path(normalized_manifest):
            return jsonify({'error': 'Manifest outside videos path'}), 403
        if not _video_rel_matches_date(normalized_manifest, date_str):
            return jsonify({'error': 'Manifest does not match requested date'}), 404
        if not _zone_matches_video_path(zone, normalized_manifest):
            return jsonify({'error': 'Manifest does not match requested zone'}), 404
        manifest_abs_path, _ = _resolve_blob_mount_path(normalized_manifest)
        if not manifest_abs_path or not os.path.exists(manifest_abs_path) or not os.path.isfile(manifest_abs_path):
            return jsonify({'error': 'Manifest not found'}), 404

        manifest_dir = os.path.dirname(normalized_manifest)
        asset_input = normalize_blob_path(asset_path)
        if not asset_input:
            return jsonify({'error': 'Invalid asset path'}), 400
        if manifest_dir and (asset_input == manifest_dir or asset_input.startswith(f"{manifest_dir}/")):
            joined_asset_rel = asset_input
        else:
            joined_asset_rel = normalize_blob_path(os.path.join(manifest_dir, asset_input).replace("\\", "/"))
        if not joined_asset_rel:
            return jsonify({'error': 'Invalid asset path'}), 400
        if manifest_dir and not joined_asset_rel.startswith(f"{manifest_dir}/") and joined_asset_rel != manifest_dir:
            return jsonify({'error': 'Asset path outside manifest directory'}), 400
        if not _blob_mount_path_matches_project(project, joined_asset_rel):
            return jsonify({'error': 'Asset outside project scope'}), 403
        if not _is_blob_video_candidate_path(joined_asset_rel):
            return jsonify({'error': 'Asset outside videos path'}), 403
        if not _video_rel_matches_date(joined_asset_rel, date_str):
            return jsonify({'error': 'Asset does not match requested date'}), 404
        if not _zone_matches_video_path(zone, joined_asset_rel):
            return jsonify({'error': 'Asset does not match requested zone'}), 404
        asset_abs_path, normalized_asset = _resolve_blob_mount_path(joined_asset_rel)
        if not asset_abs_path or not os.path.exists(asset_abs_path) or not os.path.isfile(asset_abs_path):
            return jsonify({'error': 'Asset not found'}), 404
        asset_ext = os.path.splitext(asset_abs_path)[1].lower()
        if asset_ext not in HLS_ASSET_EXTENSIONS:
            return jsonify({'error': 'Unsupported HLS asset format'}), 400

        asset_sig = get_file_signature(asset_abs_path)
        etag = build_etag("zone-hls-asset-mount", project, zone, date_str, normalized_manifest, normalized_asset, asset_sig)
        if request_etag_matches(etag):
            return make_not_modified_response(etag)
        mime_map = {
            ".m3u8": "application/vnd.apple.mpegurl",
            ".ts": "video/mp2t",
            ".m4s": "video/iso.segment",
            ".aac": "audio/aac",
            ".vtt": "text/vtt",
            ".key": "application/octet-stream",
        }
        mimetype = mime_map.get(asset_ext) or mimetypes.guess_type(asset_abs_path)[0] or "application/octet-stream"
        response = send_file(asset_abs_path, mimetype=mimetype, conditional=True)
        apply_cache_headers(response, etag)
        log_timing("get_zone_video_hls_asset", request_start, project=project, zone=zone, date=date_str, asset=normalized_asset, result="blob_mount")
        return response

    video_roots = resolve_zone_video_roots(project_layout_dir, date_str)
    if not video_roots:
        return jsonify({'error': 'No zone videos for this date'}), 404

    manifest_abs_path, normalized_manifest = _resolve_zone_video_path(project_layout_dir, manifest_path)
    if not manifest_abs_path:
        return jsonify({'error': 'Invalid manifest path'}), 400
    if os.path.splitext(normalized_manifest)[1].lower() not in HLS_MANIFEST_EXTENSIONS:
        return jsonify({'error': 'Unsupported manifest format'}), 400
    if not _video_rel_matches_date(normalized_manifest, date_str):
        return jsonify({'error': 'Manifest does not match requested date'}), 404
    if not _zone_matches_video_path(zone, normalized_manifest):
        return jsonify({'error': 'Manifest does not match requested zone'}), 404

    manifest_dir = os.path.dirname(normalized_manifest)
    asset_input = normalize_blob_path(asset_path)
    if not asset_input:
        return jsonify({'error': 'Invalid asset path'}), 400
    if manifest_dir and (asset_input == manifest_dir or asset_input.startswith(f"{manifest_dir}/")):
        joined_asset_rel = asset_input
    else:
        joined_asset_rel = os.path.normpath(os.path.join(manifest_dir, asset_input)).replace("\\", "/")
    if joined_asset_rel.startswith("../") or joined_asset_rel == "..":
        return jsonify({'error': 'Invalid asset path'}), 400
    if manifest_dir and not joined_asset_rel.startswith(f"{manifest_dir}/") and joined_asset_rel != manifest_dir:
        return jsonify({'error': 'Asset path outside manifest directory'}), 400

    asset_abs_path, normalized_asset = _resolve_zone_video_path(project_layout_dir, joined_asset_rel)
    if not asset_abs_path:
        return jsonify({'error': 'Invalid asset path'}), 400
    if not os.path.exists(asset_abs_path) or not os.path.isfile(asset_abs_path):
        return jsonify({'error': 'Asset not found'}), 404
    asset_ext = os.path.splitext(asset_abs_path)[1].lower()
    if asset_ext not in HLS_ASSET_EXTENSIONS:
        return jsonify({'error': 'Unsupported HLS asset format'}), 400

    asset_sig = get_file_signature(asset_abs_path)
    etag = build_etag("zone-hls-asset", project, zone, date_str, normalized_manifest, normalized_asset, asset_sig)
    if request_etag_matches(etag):
        return make_not_modified_response(etag)

    mime_map = {
        ".m3u8": "application/vnd.apple.mpegurl",
        ".ts": "video/mp2t",
        ".m4s": "video/iso.segment",
        ".aac": "audio/aac",
        ".vtt": "text/vtt",
        ".key": "application/octet-stream",
    }
    mimetype = mime_map.get(asset_ext) or mimetypes.guess_type(asset_abs_path)[0] or "application/octet-stream"
    response = send_file(asset_abs_path, mimetype=mimetype, conditional=True)
    apply_cache_headers(response, etag)
    log_timing("get_zone_video_hls_asset", request_start, project=project, zone=zone, date=date_str, asset=normalized_asset)
    return response


@app.route('/api/tracker/<date_str>/<tracker_id>')
def get_tracker_image(date_str, tracker_id):
    """Get individual tracker TIFF as base64 for a specific date"""
    request_start = time.perf_counter()
    # Find tracker TIFF - try multiple locations
    tracker_tif = f"{tracker_id}_boundary_spine.tif"
    
    # Find the date folder to get the date_match
    project = get_project_from_request()
    project_layout_dir = get_layout_dir(project)
    is_zone_project = project_has_zones(project)
    if is_zone_project:
        zone = normalize_zone_code(request.args.get('zone'))
        if not zone:
            return jsonify({'error': 'Zone required for this project'}), 400
        date_folder_path = find_sonrisa_date_folder(project_layout_dir, zone, date_str)
        if not date_folder_path:
            return jsonify({'error': f'Date folder not found for {zone} {date_str}'}), 404
    else:
        date_folder = f"{project}{date_str}"
        date_folder_path = os.path.join(project_layout_dir, date_folder)
    
    if not os.path.exists(date_folder_path):
        return jsonify({'error': f'Date folder not found'}), 404

    if is_zone_project:
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
        # Flight-style: Zone_N/extracted_tracker_images/*.png and tcpt_objdet_rawimg/results/Flight_*/Zone_N/extracted_tracker_images/*.png
        zone_num = zone[1:]
        for alias in zone_folder_aliases:
            zone_subdir = alias if alias.startswith("Zone_") else f"Zone_{alias[1:]}"
            tracker_paths.extend([
                os.path.join(date_folder_path, zone_subdir, "extracted_tracker_images", f"{tracker_id}.png"),
                os.path.join(date_folder_path, zone_subdir, "extracted_tracker_images", tracker_filename),
            ])
        date_folder_name = os.path.basename(date_folder_path)
        if date_folder_name.startswith("Flight_") and TCPT_OBJDET_DIR and os.path.isdir(TCPT_OBJDET_DIR):
            flight_base = re.sub(r"_\d{8}(_.*)?$", "", date_folder_name)
            tracker_paths.extend([
                os.path.join(TCPT_OBJDET_DIR, "results", flight_base, f"Zone_{zone_num}", "extracted_tracker_images", f"{tracker_id}.png"),
            ])
        for path in tracker_paths:
            if os.path.exists(path):
                base64_img = tif_to_base64(path) if path.lower().endswith(('.tif', '.tiff')) else image_to_base64(path)
                if base64_img:
                    log_timing("get_tracker_image", request_start, date=date_str, tracker=tracker_id, path=path)
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
                log_timing("get_tracker_image", request_start, date=date_str, tracker=tracker_id, path=path)
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
        
        project_layout_dir = get_layout_dir(project)
        is_zone_project = project_has_zones(project)
        if is_zone_project:
            zone = normalize_zone_code(request.args.get('zone'))
            if not zone:
                return jsonify({'error': 'Zone required for this project'}), 400
            date_folder_path = find_sonrisa_date_folder(project_layout_dir, zone, date_str)
        else:
            date_folder = f"{project}{date_str}"
            date_folder_path = os.path.join(project_layout_dir, date_folder)
        
        if not os.path.exists(date_folder_path):
            return jsonify({'error': 'Date folder not found'}), 404
        
        base_image_files = [f for f in os.listdir(date_folder_path) 
                           if f.endswith('.jpg') and 
                           not f.endswith('_web.jpg')]
        
        if not is_zone_project and not base_image_files:
            return jsonify({'error': f'No image files found for date {date_str}'}), 404
        
        # Extract date_match from the first image file found
        # Remove common suffixes to get base name
        date_match = None
        if base_image_files:
            first_image = base_image_files[0]
            date_match = first_image.replace('_stage_overlay.jpg', '').replace('_status_overlay.jpg', '').replace('_stage_status_overlay.jpg', '').replace('.jpg', '')
        
        if is_zone_project:
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
                fallback_tif_path, fallback_zone_tif, _ = find_sonrisa_zone_tif_fallback(project_layout_dir, zone)
                if fallback_tif_path and fallback_zone_tif:
                    date_folder_path = os.path.dirname(fallback_tif_path)
                    zone_tif = fallback_zone_tif
                    tif_path = fallback_tif_path
                else:
                    # New folder format: synthesize geo-transform from pre-rendered JPG
                    prerendered = find_prerendered_zone_jpg(date_folder_path, zone)
                    if not prerendered:
                        return jsonify({'error': 'TIFF file not found'}), 404
                    tif_path = prerendered
            else:
                tif_path = os.path.join(date_folder_path, zone_tif)
        else:
            tif_path = os.path.join(date_folder_path, f"{date_match}.tif")
            if not os.path.exists(tif_path):
                # Fall back to LEWISTIFS_DIR
                tif_path = os.path.join(LEWISTIFS_DIR, f"{date_match}.tif")
                if not os.path.exists(tif_path):
                    return jsonify({'error': 'TIFF file not found'}), 404

        tif_sig_click = get_file_signature(tif_path)
        try:
            with rasterio.open(tif_path) as src:
                # Convert pixel coordinates to geographic coordinates
                # Note: rasterio uses (row, col) = (y, x)
                src_transform = list(src.transform)
                tif_crs = src.crs
                if is_identity_or_missing_georef(src_transform, tif_crs):
                    raise ValueError("Missing georeference in source image")
                lon, lat = src.xy(y, x)
        except Exception:
            # Pre-rendered JPG path — synthesize transform from zone bounds
            zone_bounds_map = _zone_bounds_cached(project)
            zb = zone_bounds_map.get(zone) or [0.0, 0.0, 1.0, 1.0]
            min_lat_z, min_lon_z, max_lat_z, max_lon_z = float(zb[0]), float(zb[1]), float(zb[2]), float(zb[3])
            _syn_transform, syn_w, syn_h, tif_crs = get_synthetic_tif_metadata_cached(
                tif_path, tif_sig_click, min_lat_z, min_lon_z, max_lat_z, max_lon_z
            )
            a, _b, c, _d, e, f = _syn_transform
            lon = a * x + c
            lat = e * y + f
        
        boundaries = get_tracker_boundaries_cached(project)
        if is_zone_project:
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

