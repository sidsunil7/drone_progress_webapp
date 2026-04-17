"""
Filesystem watcher that auto-ingests new flight data into PostgreSQL.

Uses the watchdog library to monitor layout_data/ for new or modified files.
A per-folder debounce timer (default 5 s) prevents partial ingestion while
a bulk file copy is still in progress.

Usage in Flask:
    from db.file_watcher import start_watcher, stop_watcher
    start_watcher(layout_dir, cache_clear_fn)   # at app startup
    stop_watcher()                               # at app shutdown / atexit
"""

import logging
import os
import re
import threading
import time

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
    _WATCHDOG_AVAILABLE = True
except ImportError:
    _WATCHDOG_AVAILABLE = False

from db.ingest_service import ingest_flight_folder, ingest_project, parse_folder

logger = logging.getLogger(__name__)

# Only react to files we know contain tracker data
_INTERESTING_PATTERNS = re.compile(
    r"(tracker_status.*\.csv|zone_status\.json|tracker_status[/\\].*\.json)$",
    re.IGNORECASE,
)

_DEBOUNCE_SECONDS = float(os.environ.get("INGEST_DEBOUNCE_SECONDS", "5"))

# Shared state
_observer: "Observer | None" = None
_debounce_thread: "threading.Thread | None" = None
_stop_event = threading.Event()


class _FlightFolderHandler(FileSystemEventHandler):
    """Watches layout_data/<project>/<flight_folder>/ for file events.

    Tracks which (project, folder) pairs have pending work using debounce
    timestamps.  A background thread flushes them after DEBOUNCE_SECONDS of
    quiet activity.
    """

    def __init__(self, layout_dir: str, cache_clear_fn):
        super().__init__()
        self._layout_dir = os.path.abspath(layout_dir)
        self._cache_clear_fn = cache_clear_fn
        self._pending: dict[tuple[str, str], float] = {}  # (project, folder) -> last_event_time
        self._lock = threading.Lock()

    # ── watchdog callbacks ───────────────────────────────────────────────────

    def on_created(self, event):
        self._handle(event.src_path, event.is_directory)

    def on_modified(self, event):
        self._handle(event.src_path, event.is_directory)

    def on_moved(self, event):
        self._handle(event.dest_path, event.is_directory)

    # ── internal ─────────────────────────────────────────────────────────────

    def _handle(self, path: str, is_directory: bool = False):
        try:
            rel = os.path.relpath(path, self._layout_dir)
        except ValueError:
            return  # path outside layout_dir (Windows drive letter mismatch)

        parts = rel.replace("\\", "/").split("/")
        if len(parts) < 2:
            return

        project = parts[0]
        folder = parts[1]

        # Ignore hidden / app-data folders
        if project.startswith("_") or folder.startswith("_"):
            return

        # Check that the folder looks like a real flight (has a date)
        flight_date, _ = parse_folder(folder)
        if not flight_date:
            return

        # For file events, only react to files we care about.
        # Directory events (new flight folder created) always queue an ingest.
        if not is_directory:
            filename = parts[-1] if parts else ""
            if not _INTERESTING_PATTERNS.search(rel) and filename:
                return

        with self._lock:
            self._pending[(project, folder)] = time.monotonic()
            logger.debug("ingest: queued %s/%s (debounce)", project, folder)

    def flush_pending(self):
        """Called by the debounce thread: ingest folders that have been quiet."""
        now = time.monotonic()
        to_ingest = []
        with self._lock:
            done = []
            for key, last_event in self._pending.items():
                if now - last_event >= _DEBOUNCE_SECONDS:
                    to_ingest.append(key)
                    done.append(key)
            for key in done:
                del self._pending[key]

        for project, folder in to_ingest:
            folder_path = os.path.join(self._layout_dir, project, folder)
            if not os.path.isdir(folder_path):
                logger.warning("ingest: folder no longer exists: %s", folder_path)
                continue
            try:
                logger.info("ingest: starting %s/%s", project, folder)
                result = ingest_flight_folder(project, folder, folder_path)
                logger.info(
                    "ingest: finished %s/%s — zones=%d csv=%d json=%d",
                    project, folder,
                    result["zones"], result["csv_trackers"], result["json_trackers"],
                )
                if self._cache_clear_fn:
                    try:
                        self._cache_clear_fn()
                        logger.debug("ingest: LRU caches cleared after %s/%s", project, folder)
                    except Exception:
                        logger.exception("ingest: cache_clear_fn raised")
            except Exception:
                logger.exception("ingest: error ingesting %s/%s", project, folder)


def _debounce_loop(handler: "_FlightFolderHandler", stop_event: threading.Event):
    """Background thread: periodically flushes debounced ingest queue."""
    while not stop_event.is_set():
        time.sleep(1)
        try:
            handler.flush_pending()
        except Exception:
            logger.exception("ingest: unexpected error in debounce loop")


def _startup_scan(layout_dir: str, cache_clear_fn):
    """Background thread: scan all existing flight folders on startup.

    Runs once immediately after the watcher starts so any data added while
    the server was down is ingested before the app serves requests.
    """
    logger.info("ingest: startup scan beginning for %s", layout_dir)
    total_folders = 0
    total_trackers = 0

    try:
        if not os.path.isdir(layout_dir):
            return
        for project in sorted(os.listdir(layout_dir)):
            project_dir = os.path.join(layout_dir, project)
            if not os.path.isdir(project_dir) or project.startswith("_"):
                continue
            result = ingest_project(project, project_dir)
            total_folders += result["folders"]
            total_trackers += result["csv_trackers"] + result["json_trackers"]

        if cache_clear_fn:
            try:
                cache_clear_fn()
            except Exception:
                logger.exception("ingest: cache_clear_fn raised during startup scan")

        logger.info(
            "ingest: startup scan complete — %d folders, %d trackers ingested",
            total_folders, total_trackers,
        )
    except Exception:
        logger.exception("ingest: startup scan failed")


# ── Public API ───────────────────────────────────────────────────────────────

def start_watcher(layout_dir: str, cache_clear_fn=None):
    """Start the filesystem watcher daemon thread.

    Args:
        layout_dir:      Absolute path to the layout_data directory.
        cache_clear_fn:  Zero-argument callable that clears all LRU caches.
                         Called automatically after each successful ingest.
    """
    global _observer, _debounce_thread, _stop_event

    if not _WATCHDOG_AVAILABLE:
        logger.warning(
            "watchdog library not installed — auto-ingest disabled. "
            "Run: pip install watchdog"
        )
        return

    if not os.path.isdir(layout_dir):
        logger.warning("ingest: layout_dir does not exist yet: %s", layout_dir)

    _stop_event.clear()

    handler = _FlightFolderHandler(layout_dir, cache_clear_fn)

    _observer = Observer()
    _observer.schedule(handler, layout_dir, recursive=True)
    _observer.daemon = True
    _observer.start()
    logger.info("ingest: watchdog observer started on %s", layout_dir)

    _debounce_thread = threading.Thread(
        target=_debounce_loop,
        args=(handler, _stop_event),
        name="ingest-debounce",
        daemon=True,
    )
    _debounce_thread.start()
    logger.info("ingest: debounce thread started (interval=%.1fs)", _DEBOUNCE_SECONDS)

    # Scan all existing folders in a background thread so the Flask server
    # is not blocked at startup while catching up on any data added while it
    # was down.
    startup_thread = threading.Thread(
        target=_startup_scan,
        args=(layout_dir, cache_clear_fn),
        name="ingest-startup-scan",
        daemon=True,
    )
    startup_thread.start()
    logger.info("ingest: startup scan launched in background")


def stop_watcher():
    """Stop the filesystem watcher (call on app shutdown)."""
    global _observer, _debounce_thread

    _stop_event.set()

    if _observer is not None:
        try:
            _observer.stop()
            _observer.join(timeout=5)
        except Exception:
            logger.exception("ingest: error stopping observer")
        _observer = None

    if _debounce_thread is not None:
        _debounce_thread.join(timeout=5)
        _debounce_thread = None

    logger.info("ingest: watcher stopped")
