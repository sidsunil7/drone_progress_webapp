"""
Auto-ingest service: upserts new flight folder data into PostgreSQL.

This module contains the same core logic as migration scripts 03 and 04 but
uses the Flask app's connection pool (db_service) instead of raw psycopg2
connections, and is designed to be called at runtime when new files appear.

All upserts use ON CONFLICT clauses so it is safe to re-process the same
folder multiple times.
"""

import csv
import glob
import json
import logging
import os
import re

from db.db_service import get_db, release_db

logger = logging.getLogger(__name__)

# ── Folder / zone parsing (mirrors 03_migrate_zones_and_flights.py) ─────────

_DATE_RE = re.compile(r"(\d{8})")
_ZONE_TOKEN_RE = re.compile(r"^[A-Za-z]?(\d{1,2})$")


def normalize_zone_code(zone_str):
    """Convert a raw zone token to the canonical G-prefixed format.

    Examples:  '1' -> 'G01',  '13' -> 'G13',  'G01' -> 'G01'
    """
    zone_str = str(zone_str).strip()
    if zone_str.upper().startswith("G"):
        # Already prefixed — just ensure 2-digit number
        digits = zone_str[1:].lstrip("0") or "0"
        return "G" + digits.zfill(2)
    # Bare number
    digits = zone_str.lstrip("0") or "0"
    return "G" + digits.zfill(2)


def parse_folder(folder_name):
    """Return (flight_date_iso, sorted_zone_code_list) from a folder name.

    Handles patterns like:
      Flight_1-2-8-9-10_20260209
      G1_2_8_9_10_74m_70_80_ovrlp_20260212
      Flight_13-14-19-21_20260319

    Returns (None, []) when no 8-digit date is found.
    """
    date_match = _DATE_RE.search(folder_name)
    if not date_match:
        return None, []

    date_str = date_match.group(1)
    flight_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

    date_pos = date_match.start()
    before_date = folder_name[:date_pos]

    tokens = re.split(r"[-_]", before_date)
    zones = []
    for tok in tokens:
        tok = tok.strip()
        if not tok:
            continue
        if tok.lower() in ("flight", "ovrlp", "overlp", "74m", "70", "80"):
            continue
        m = _ZONE_TOKEN_RE.match(tok)
        if m:
            zones.append(m.group(1))
        elif tok.isdigit() and len(tok) <= 2:
            zones.append(tok)

    return flight_date, sorted(set(zones))


def discover_zone_subdirs(flight_dir):
    """Return zone code strings found in Zone_N or zone_N subdirectories."""
    zones = []
    try:
        for entry in os.listdir(flight_dir):
            m = re.match(r"[Zz]one_(\d+)", entry)
            if m and os.path.isdir(os.path.join(flight_dir, entry)):
                zones.append(m.group(1))
    except OSError:
        pass
    return zones


def discover_tracker_zones(flight_dir):
    """Return zone codes from trackers/GN subdirectories (fallback)."""
    zones = []
    trackers_dir = os.path.join(flight_dir, "trackers")
    if os.path.isdir(trackers_dir):
        try:
            for entry in os.listdir(trackers_dir):
                m = re.match(r"[Gg](\d+)", entry)
                if m:
                    zones.append(m.group(1))
        except OSError:
            pass
    return zones


# ── Tracker status helpers (mirrors 04_migrate_tracker_status.py) ───────────

INSTALL_COLS = [
    "pile_installation",
    "lower_journal_installation",
    "slew_drive_installation",
    "torque_tube_installation",
    "torque_tube_coupler_installation",
    "upper_journal_installation",
    "module_rail_installation",
    "pony_panel_installation",
    "solar_module_installation",
]

_STAGE_KEYS = ["pile", "torque_tube", "module_rails", "solar_panel"]
_STAGE_INSTALL_MAP = {
    "pile_installation": "pile",
    "torque_tube_installation": "torque_tube",
    "module_rail_installation": "module_rails",
    "solar_module_installation": "solar_panel",
}


def normalize_status(val):
    if not val:
        return "not_started"
    return val.strip().lower().replace(" ", "_")


def compute_current_stage(statuses):
    """Derive (current_stage, current_status) from installation column dict."""
    mapped = {stage: normalize_status(statuses.get(col))
              for col, stage in _STAGE_INSTALL_MAP.items()}

    for stage in reversed(_STAGE_KEYS):
        if mapped[stage] == "in_progress":
            return stage, "in_progress"

    for stage in reversed(_STAGE_KEYS):
        if mapped[stage] == "completed":
            idx = _STAGE_KEYS.index(stage)
            if idx < len(_STAGE_KEYS) - 1:
                return _STAGE_KEYS[idx + 1], "not_started"
            return stage, "completed"

    return "pile", "not_started"


# ── Low-level DB upserts (use a caller-managed cursor) ──────────────────────

def _upsert_zone(cur, project_id, zone_code):
    cur.execute(
        """
        INSERT INTO dim_zone (project_id, zone_code, zone_label)
        VALUES (%s, %s, %s)
        ON CONFLICT (project_id, zone_code) DO NOTHING
        """,
        (project_id, zone_code, f"Zone {zone_code}"),
    )


def _upsert_flight(cur, project_id, folder_name, flight_date):
    """Upsert fact_flight and return flight_id UUID."""
    cur.execute(
        """
        INSERT INTO fact_flight (project_id, flight_date, folder_name, flight_label)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (project_id, folder_name) DO UPDATE SET
            flight_date  = EXCLUDED.flight_date,
            flight_label = EXCLUDED.flight_label
        RETURNING flight_id
        """,
        (project_id, flight_date, folder_name, folder_name),
    )
    return cur.fetchone()[0]


def _upsert_flight_zone(cur, flight_id, zone_id):
    cur.execute(
        """
        INSERT INTO fact_flight_zone (flight_id, zone_id)
        VALUES (%s, %s)
        ON CONFLICT (flight_id, zone_id) DO NOTHING
        """,
        (flight_id, zone_id),
    )


def _get_zone_id(cur, project_id, zone_code):
    cur.execute(
        "SELECT zone_id FROM dim_zone WHERE project_id = %s AND zone_code = %s",
        (project_id, zone_code),
    )
    row = cur.fetchone()
    return row[0] if row else None


def _get_flight_id(cur, project_id, folder_name):
    cur.execute(
        "SELECT flight_id FROM fact_flight WHERE project_id = %s AND folder_name = %s",
        (project_id, folder_name),
    )
    row = cur.fetchone()
    return row[0] if row else None


def _upsert_tracker_status(cur, flight_id, zone_id, tracker_name, statuses,
                           source_format, pipeline_run_id=None):
    stage, status = compute_current_stage(statuses)
    cur.execute(
        """
        INSERT INTO fact_tracker_status
            (flight_id, zone_id, pipeline_run_id, tracker_name, source_format,
             pile_installation, lower_journal_installation, slew_drive_installation,
             torque_tube_installation, torque_tube_coupler_installation,
             upper_journal_installation, module_rail_installation,
             pony_panel_installation, solar_module_installation,
             current_stage, current_status)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (flight_id, zone_id, tracker_name) DO UPDATE SET
            pipeline_run_id                  = COALESCE(EXCLUDED.pipeline_run_id, fact_tracker_status.pipeline_run_id),
            source_format                    = EXCLUDED.source_format,
            pile_installation                = EXCLUDED.pile_installation,
            lower_journal_installation       = EXCLUDED.lower_journal_installation,
            slew_drive_installation          = EXCLUDED.slew_drive_installation,
            torque_tube_installation         = EXCLUDED.torque_tube_installation,
            torque_tube_coupler_installation = EXCLUDED.torque_tube_coupler_installation,
            upper_journal_installation       = EXCLUDED.upper_journal_installation,
            module_rail_installation         = EXCLUDED.module_rail_installation,
            pony_panel_installation          = EXCLUDED.pony_panel_installation,
            solar_module_installation        = EXCLUDED.solar_module_installation,
            current_stage                    = EXCLUDED.current_stage,
            current_status                   = EXCLUDED.current_status
        """,
        (
            flight_id, zone_id, pipeline_run_id, tracker_name, source_format,
            normalize_status(statuses.get("pile_installation")),
            normalize_status(statuses.get("lower_journal_installation")),
            normalize_status(statuses.get("slew_drive_installation")),
            normalize_status(statuses.get("torque_tube_installation")),
            normalize_status(statuses.get("torque_tube_coupler_installation")),
            normalize_status(statuses.get("upper_journal_installation")),
            normalize_status(statuses.get("module_rail_installation")),
            normalize_status(statuses.get("pony_panel_installation")),
            normalize_status(statuses.get("solar_module_installation")),
            stage, status,
        ),
    )


def _upsert_tracker_status_no_overwrite(cur, flight_id, zone_id, tracker_name, statuses,
                                        source_format, pipeline_run_id=None):
    """Same as _upsert_tracker_status but uses DO NOTHING on conflict.
    Used for spine/boundary entries so regular data always takes precedence."""
    stage, status = compute_current_stage(statuses)
    cur.execute(
        """
        INSERT INTO fact_tracker_status
            (flight_id, zone_id, pipeline_run_id, tracker_name, source_format,
             pile_installation, lower_journal_installation, slew_drive_installation,
             torque_tube_installation, torque_tube_coupler_installation,
             upper_journal_installation, module_rail_installation,
             pony_panel_installation, solar_module_installation,
             current_stage, current_status)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (flight_id, zone_id, tracker_name) DO NOTHING
        """,
        (
            flight_id, zone_id, pipeline_run_id, tracker_name, source_format,
            normalize_status(statuses.get("pile_installation")),
            normalize_status(statuses.get("lower_journal_installation")),
            normalize_status(statuses.get("slew_drive_installation")),
            normalize_status(statuses.get("torque_tube_installation")),
            normalize_status(statuses.get("torque_tube_coupler_installation")),
            normalize_status(statuses.get("upper_journal_installation")),
            normalize_status(statuses.get("module_rail_installation")),
            normalize_status(statuses.get("pony_panel_installation")),
            normalize_status(statuses.get("solar_module_installation")),
            stage, status,
        ),
    )


def _upsert_pipeline_run(cur, flight_id, zone_id, data):
    """Insert/update a fact_pipeline_run row and return its run_id."""
    seed = data.get("seed_tracker", "")
    pano = data.get("panorama", {})
    canvas = pano.get("canvas_size", [None, None])
    timings = data.get("timings_s", {})

    cur.execute(
        """
        INSERT INTO fact_pipeline_run
            (flight_id, zone_id, seed_tracker_name,
             canvas_width, canvas_height, num_source_images, total_detections, source_images,
             timing_image_selection_s, timing_stitch_and_axes_s, timing_detection_s,
             timing_assignment_and_bbox_s, timing_status_calculation_s)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (flight_id, zone_id, seed_tracker_name) DO UPDATE SET
            canvas_width                 = EXCLUDED.canvas_width,
            canvas_height                = EXCLUDED.canvas_height,
            num_source_images            = EXCLUDED.num_source_images,
            total_detections             = EXCLUDED.total_detections,
            source_images                = EXCLUDED.source_images,
            timing_image_selection_s     = EXCLUDED.timing_image_selection_s,
            timing_stitch_and_axes_s     = EXCLUDED.timing_stitch_and_axes_s,
            timing_detection_s           = EXCLUDED.timing_detection_s,
            timing_assignment_and_bbox_s = EXCLUDED.timing_assignment_and_bbox_s,
            timing_status_calculation_s  = EXCLUDED.timing_status_calculation_s
        RETURNING run_id
        """,
        (
            flight_id, zone_id, seed,
            canvas[0] if len(canvas) > 0 else None,
            canvas[1] if len(canvas) > 1 else None,
            pano.get("num_source_images"),
            pano.get("total_detections"),
            json.dumps(pano.get("source_images", [])),
            timings.get("image_selection"),
            timings.get("stitch_and_axes"),
            timings.get("detection"),
            timings.get("assignment_and_bbox"),
            timings.get("status_calculation"),
        ),
    )
    row = cur.fetchone()
    return row[0] if row else None


# ── CSV + JSON ingestors ─────────────────────────────────────────────────────

def _ingest_csv_for_folder(cur, project_id, folder_name, folder_path):
    """Ingest all tracker_status*.csv files under folder_path. Returns row count."""
    csv_files = glob.glob(
        os.path.join(folder_path, "**", "tracker_status*.csv"), recursive=True
    )
    total = 0

    for csv_path in sorted(csv_files):
        rel = os.path.relpath(csv_path, folder_path)
        parts = rel.replace("\\", "/").split("/")

        zone_code = None
        for p in parts:
            m = re.match(r"[Zz]one_(\d+)", p)
            if m:
                zone_code = normalize_zone_code(m.group(1))
                break
            m = re.match(r"[Gg](\d+)", p)
            if m:
                zone_code = normalize_zone_code(m.group(1))
                break

        if not zone_code:
            logger.warning("ingest: skip CSV (no zone in path): %s", rel)
            continue

        flight_id = _get_flight_id(cur, project_id, folder_name)
        zone_id = _get_zone_id(cur, project_id, zone_code)
        if not flight_id or not zone_id:
            logger.warning(
                "ingest: skip CSV (flight/zone not in DB): folder=%s zone=%s",
                folder_name, zone_code,
            )
            continue

        count = 0
        try:
            with open(csv_path, "r", encoding="utf-8") as f:
                all_rows = list(csv.DictReader(f))

            # Two-pass: regular entries first (DO UPDATE), spine entries second
            # (DO NOTHING) so regular data always wins when both exist.
            # Spine-only CSVs (common in early flights) are handled by pass 2.
            for row in all_rows:
                tracker_name = row.get("tracker_id", "").strip()
                if not tracker_name:
                    continue
                tl = tracker_name.lower()
                if tl.endswith("_boundary_spine") or tl.endswith("_boundary"):
                    continue
                statuses = {col: row.get(col) for col in INSTALL_COLS}
                _upsert_tracker_status(cur, flight_id, zone_id, tracker_name, statuses, "csv")
                count += 1

            for row in all_rows:
                tracker_name = row.get("tracker_id", "").strip()
                if not tracker_name:
                    continue
                tl = tracker_name.lower()
                if tl.endswith("_boundary_spine"):
                    canonical = tracker_name[:-15]
                elif tl.endswith("_boundary"):
                    canonical = tracker_name[:-9]
                else:
                    continue
                if not canonical:
                    continue
                statuses = {col: row.get(col) for col in INSTALL_COLS}
                _upsert_tracker_status_no_overwrite(cur, flight_id, zone_id, canonical, statuses, "csv")
                count += 1
        except (OSError, csv.Error) as exc:
            logger.error("ingest: error reading CSV %s: %s", csv_path, exc)
            continue

        total += count
        logger.info("ingest: CSV %s -> %d trackers", rel, count)

    return total


def _ingest_json_for_folder(cur, project_id, folder_name, folder_path):
    """Ingest zone_status.json and tracker_status/*.json files. Returns row count."""
    total = 0

    for root, _dirs, files in os.walk(folder_path):
        rel_root = os.path.relpath(root, folder_path)
        parts = rel_root.replace("\\", "/").split("/")

        zone_code = None
        for p in parts:
            m = re.match(r"[Zz]one_(\d+)", p)
            if m:
                zone_code = normalize_zone_code(m.group(1))
                break

        if not zone_code:
            continue

        flight_id = _get_flight_id(cur, project_id, folder_name)
        zone_id = _get_zone_id(cur, project_id, zone_code)
        if not flight_id or not zone_id:
            continue

        # Per-seed-tracker JSON files
        tracker_status_dir = os.path.join(root, "tracker_status")
        if os.path.isdir(tracker_status_dir):
            for fname in sorted(os.listdir(tracker_status_dir)):
                if not fname.endswith(".json"):
                    continue
                fpath = os.path.join(tracker_status_dir, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except (OSError, json.JSONDecodeError) as exc:
                    logger.error("ingest: error reading %s: %s", fpath, exc)
                    continue

                run_id = _upsert_pipeline_run(cur, flight_id, zone_id, data)
                trackers_list = data.get("trackers", [])
                # Pass 1: regular entries
                for tracker in trackers_list:
                    tname = tracker.get("tracker_name", "").strip()
                    if not tname:
                        continue
                    tl = tname.lower()
                    if tl.endswith("_boundary_spine") or tl.endswith("_boundary"):
                        continue
                    statuses = tracker.get("construction_status", {})
                    _upsert_tracker_status(cur, flight_id, zone_id, tname, statuses, "json", run_id)
                    total += 1
                # Pass 2: spine/boundary entries (normalized, DO NOTHING)
                for tracker in trackers_list:
                    tname = tracker.get("tracker_name", "").strip()
                    if not tname:
                        continue
                    tl = tname.lower()
                    if tl.endswith("_boundary_spine"):
                        canonical = tname[:-15]
                    elif tl.endswith("_boundary"):
                        canonical = tname[:-9]
                    else:
                        continue
                    if not canonical:
                        continue
                    statuses = tracker.get("construction_status", {})
                    _upsert_tracker_status_no_overwrite(cur, flight_id, zone_id, canonical, statuses, "json", run_id)
                    total += 1

        # Aggregated zone_status.json
        zone_status_path = os.path.join(root, "zone_status.json")
        if os.path.isfile(zone_status_path):
            try:
                with open(zone_status_path, "r", encoding="utf-8") as f:
                    zdata = json.load(f)
            except (OSError, json.JSONDecodeError) as exc:
                logger.error("ingest: error reading %s: %s", zone_status_path, exc)
                continue

            trackers = zdata.get("trackers", {})
            if isinstance(trackers, dict):
                items = list(trackers.items())
            elif isinstance(trackers, list):
                items = [(t.get("tracker_name", t.get("tracker_id", "")), t) for t in trackers]
            else:
                items = []

            for tname, tinfo in items:
                tname = tname.strip() if isinstance(tname, str) else str(tname)
                if not tname:
                    continue
                tl = tname.lower()
                is_spine = tl.endswith("_boundary_spine") or tl.endswith("_boundary")
                if tl.endswith("_boundary_spine"):
                    canonical = tname[:-15]
                elif tl.endswith("_boundary"):
                    canonical = tname[:-9]
                else:
                    canonical = tname
                if not canonical:
                    continue
                statuses = tinfo if isinstance(tinfo, dict) else {}
                if "construction_status" in statuses:
                    statuses = statuses["construction_status"]
                if is_spine:
                    _upsert_tracker_status_no_overwrite(cur, flight_id, zone_id, canonical, statuses, "json")
                else:
                    _upsert_tracker_status(cur, flight_id, zone_id, canonical, statuses, "json")
                total += 1

    return total


# ── Public entry point ───────────────────────────────────────────────────────

def ingest_flight_folder(project_name, folder_name, folder_path):
    """Parse and upsert a single flight folder into the database.

    Steps performed (all in one transaction):
      1. Resolve project_id
      2. Parse flight date and zone codes from folder name + subdirs
      3. Upsert dim_zone rows
      4. Upsert fact_flight row
      5. Upsert fact_flight_zone links
      6. Ingest CSV tracker statuses
      7. Ingest JSON tracker statuses + pipeline runs

    Returns a dict with counts:
      { "zones": int, "csv_trackers": int, "json_trackers": int }
    or raises on fatal error.
    """
    conn = get_db()
    try:
        cur = conn.cursor()

        # 1. Project
        cur.execute(
            "SELECT project_id FROM dim_project WHERE project_name = %s",
            (project_name,),
        )
        row = cur.fetchone()
        if not row:
            logger.warning("ingest: unknown project %r, skipping %s", project_name, folder_name)
            cur.close()
            return {"zones": 0, "csv_trackers": 0, "json_trackers": 0}
        project_id = row[0]

        # 2. Parse folder name
        flight_date, folder_zones = parse_folder(folder_name)
        if not flight_date:
            logger.warning("ingest: no date in folder name %r, skipping", folder_name)
            cur.close()
            return {"zones": 0, "csv_trackers": 0, "json_trackers": 0}

        subdir_zones = discover_zone_subdirs(folder_path)
        combined_zones = sorted(
            {normalize_zone_code(z) for z in set(folder_zones) | set(subdir_zones)}
        )
        if not combined_zones:
            combined_zones = sorted(
                {normalize_zone_code(z) for z in discover_tracker_zones(folder_path)}
            )

        # 3. Upsert zones
        for zc in combined_zones:
            _upsert_zone(cur, project_id, zc)

        # 4. Upsert flight
        flight_id = _upsert_flight(cur, project_id, folder_name, flight_date)

        # 5. Upsert flight-zone links
        for zc in combined_zones:
            zone_id = _get_zone_id(cur, project_id, zc)
            if zone_id:
                _upsert_flight_zone(cur, flight_id, zone_id)

        # 6 & 7. Ingest tracker data
        csv_count = _ingest_csv_for_folder(cur, project_id, folder_name, folder_path)
        json_count = _ingest_json_for_folder(cur, project_id, folder_name, folder_path)

        conn.commit()
        cur.close()

        logger.info(
            "ingest: %s/%s done — zones=%d csv_trackers=%d json_trackers=%d",
            project_name, folder_name, len(combined_zones), csv_count, json_count,
        )
        return {"zones": len(combined_zones), "csv_trackers": csv_count, "json_trackers": json_count}

    except Exception:
        conn.rollback()
        logger.exception("ingest: transaction rolled back for %s/%s", project_name, folder_name)
        raise
    finally:
        release_db(conn)


def ingest_project(project_name, project_dir):
    """Scan all flight folders under project_dir and ingest each one.

    Returns aggregated counts.
    """
    totals = {"zones": 0, "csv_trackers": 0, "json_trackers": 0, "folders": 0}
    if not os.path.isdir(project_dir):
        return totals

    for folder in sorted(os.listdir(project_dir)):
        folder_path = os.path.join(project_dir, folder)
        if not os.path.isdir(folder_path) or folder.startswith("_"):
            continue
        flight_date, _ = parse_folder(folder)
        if not flight_date:
            continue
        try:
            result = ingest_flight_folder(project_name, folder, folder_path)
            for k in ("zones", "csv_trackers", "json_trackers"):
                totals[k] += result[k]
            totals["folders"] += 1
        except Exception:
            logger.exception("ingest: failed for %s/%s", project_name, folder)

    return totals
