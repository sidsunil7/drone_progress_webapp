#!/usr/bin/env python3
"""
db/setup.py — Full database reset and fresh seed.

Run this to wipe and rebuild the drone_progress database from scratch:

    python3 db/setup.py

Steps performed:
  1.  Drop + recreate the `drone_progress` database
  2.  Apply init.sql   (extensions, roles)
  3.  Apply schema.sql (all tables + indexes)
  4.  Apply seed_reference.sql (dim_stage, dim_installation_step)
  5.  For every project found under layout_data/:
        a. dim_project          ← project_settings.json
        b. dim_site + dim_tracker ← *_construction_AI*.json
        c. dim_zone + fact_flight + fact_flight_zone  ← flight-folder names + subdirs
        d. fact_tracker_status  ← tracker_status*.csv / zone_status.json
        e. fact_pipeline_run    ← tracker_status/*.json  (per-seed JSON)
        f. fact_manpower_daily + fact_stage_milestone ← manpower_data.json
        g. fact_tracker_extraction ← extracted_tracker_images/target_plus_neighbors_crops.json
"""

import csv
import glob
import json
import os
import re
import sys
import time

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

# ── Paths ────────────────────────────────────────────────────────────────────

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "db_config.json")
INIT_SQL = os.path.join(PROJECT_ROOT, "db", "init.sql")
SCHEMA_SQL = os.path.join(PROJECT_ROOT, "db", "schema.sql")
SEED_SQL = os.path.join(PROJECT_ROOT, "db", "seed_reference.sql")
LAYOUT_DIR = os.path.join(PROJECT_ROOT, "layout_data")


# ── Config ───────────────────────────────────────────────────────────────────

def _load_config():
    with open(DB_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _admin_conn(cfg, dbname="template1"):
    """Connect to a maintenance database (for DROP/CREATE).
    Uses template1 because this container was created without a 'postgres' db."""
    return psycopg2.connect(
        host=cfg.get("host", "localhost"),
        port=int(cfg.get("port", 5432)),
        dbname=dbname,
        user=cfg.get("user", "admin"),
        password=cfg.get("password", ""),
    )


def _app_conn(cfg):
    """Connect to the drone_progress database."""
    return psycopg2.connect(
        host=cfg.get("host", "localhost"),
        port=int(cfg.get("port", 5432)),
        dbname=cfg.get("dbname", "drone_progress"),
        user=cfg.get("user", "admin"),
        password=cfg.get("password", ""),
    )


# ── Step 1: Drop + recreate database ─────────────────────────────────────────

def reset_database(cfg):
    dbname = cfg.get("dbname", "drone_progress")
    print(f"\n{'─'*60}")
    print(f"  Step 1 — Drop and recreate '{dbname}'")
    print(f"{'─'*60}")

    conn = _admin_conn(cfg, dbname="template1")
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()

    # Terminate all existing connections to the target DB
    cur.execute(
        """
        SELECT pg_terminate_backend(pid)
        FROM pg_stat_activity
        WHERE datname = %s AND pid <> pg_backend_pid()
        """,
        (dbname,),
    )
    terminated = cur.rowcount
    if terminated:
        print(f"  Terminated {terminated} existing connection(s).")

    cur.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
    print(f"  Dropped database '{dbname}'.")

    cur.execute(f'CREATE DATABASE "{dbname}"')
    print(f"  Created database '{dbname}'.")

    cur.close()
    conn.close()


# ── Step 2-4: Apply SQL files ─────────────────────────────────────────────────

def apply_sql_file(conn, path, label):
    print(f"\n  Applying {label} …", end=" ", flush=True)
    with open(path, "r", encoding="utf-8") as f:
        sql = f.read()
    cur = conn.cursor()
    cur.execute(sql)
    conn.commit()
    cur.close()
    print("done.")


# ── Normalisation helpers ─────────────────────────────────────────────────────

_DATE_RE = re.compile(r"(\d{8})")
_ZONE_TOKEN_RE = re.compile(r"^[A-Za-z]?(\d{1,2})$")

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
STAGE_KEY_TO_ID = {"pile": 1, "torque_tube": 2, "module_rails": 3, "solar_panel": 4}


def _normalize_zone_code(zone_str):
    zone_str = str(zone_str).strip()
    if zone_str.upper().startswith("G"):
        digits = zone_str[1:].lstrip("0") or "0"
        return "G" + digits.zfill(2)
    digits = zone_str.lstrip("0") or "0"
    return "G" + digits.zfill(2)


def _normalize_status(val):
    if not val:
        return "not_started"
    return val.strip().lower().replace(" ", "_")


def _compute_current_stage(statuses):
    mapped = {stage: _normalize_status(statuses.get(col))
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


def _num(val):
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _int(val):
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _parse_folder(folder_name):
    """Return (flight_date_iso, [zone_number_strings]) from a folder name."""
    dm = _DATE_RE.search(folder_name)
    if not dm:
        return None, []
    ds = dm.group(1)
    flight_date = f"{ds[:4]}-{ds[4:6]}-{ds[6:8]}"
    before = folder_name[:dm.start()]
    tokens = re.split(r"[-_]", before)
    zones = []
    for tok in tokens:
        tok = tok.strip()
        if not tok or tok.lower() in ("flight", "ovrlp", "overlp", "74m", "70", "80"):
            continue
        m = _ZONE_TOKEN_RE.match(tok)
        if m:
            zones.append(m.group(1))
        elif tok.isdigit() and len(tok) <= 2:
            zones.append(tok)
    return flight_date, sorted(set(zones))


def _discover_zone_subdirs(flight_dir):
    zones = []
    try:
        for entry in os.listdir(flight_dir):
            m = re.match(r"[Zz]one_(\d+)", entry)
            if m and os.path.isdir(os.path.join(flight_dir, entry)):
                zones.append(m.group(1))
    except OSError:
        pass
    return zones


def _discover_tracker_zones(flight_dir):
    zones = []
    trackers_dir = os.path.join(flight_dir, "trackers")
    if os.path.isdir(trackers_dir):
        try:
            for entry in os.listdir(trackers_dir):
                m = re.match(r"[Gg](\d+)", entry)
                if m and os.path.isdir(os.path.join(trackers_dir, entry)):
                    zones.append(m.group(1))
        except OSError:
            pass
    return zones


def _find_construction_json(project_dir):
    for pat in ["*_construction_AI.json", "*_construction_AI_*.json", "*corrected*.json"]:
        matches = glob.glob(os.path.join(project_dir, pat))
        if matches:
            return matches[0]
    return None


# ── Per-project seeders ───────────────────────────────────────────────────────

def seed_project(cur, conn, project_name, project_dir):
    print(f"\n  Project: {project_name}")

    # ── a. dim_project ────────────────────────────────────────────────────────
    settings_path = os.path.join(project_dir, "_app_data", "project_settings.json")
    if os.path.isfile(settings_path):
        with open(settings_path, "r", encoding="utf-8") as f:
            s = json.load(f)
        cur.execute(
            """
            INSERT INTO dim_project
                (project_name, start_date, end_date, hours_per_day,
                 modules_per_tracker, mw_per_tracker, working_days,
                 created_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (project_name) DO UPDATE SET
                start_date          = EXCLUDED.start_date,
                end_date            = EXCLUDED.end_date,
                hours_per_day       = EXCLUDED.hours_per_day,
                modules_per_tracker = EXCLUDED.modules_per_tracker,
                mw_per_tracker      = EXCLUDED.mw_per_tracker,
                working_days        = EXCLUDED.working_days,
                updated_at          = EXCLUDED.updated_at
            """,
            (
                s.get("project_name", project_name),
                s.get("project_start_date"),
                s.get("project_end_date"),
                s.get("hours_per_day"),
                s.get("modules_per_tracker"),
                s.get("mw_per_tracker"),
                s.get("working_days"),
                s.get("created_at"),
                s.get("updated_at"),
            ),
        )
    else:
        cur.execute(
            "INSERT INTO dim_project (project_name) VALUES (%s) ON CONFLICT (project_name) DO NOTHING",
            (project_name,),
        )

    cur.execute("SELECT project_id FROM dim_project WHERE project_name = %s",
                (s.get("project_name", project_name) if os.path.isfile(settings_path) else project_name,))
    project_id = cur.fetchone()[0]
    print(f"    dim_project: {project_name} ({project_id})")

    # ── b. dim_site + dim_tracker ─────────────────────────────────────────────
    json_path = _find_construction_json(project_dir)
    if json_path:
        with open(json_path, "r", encoding="utf-8") as f:
            cdata = json.load(f)

        site = cdata.get("siteDetails", {})
        if site:
            cur.execute(
                """
                INSERT INTO dim_site (project_id, site_name, latitude, longitude, tracker_product_type)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING
                """,
                (
                    project_id,
                    site.get("siteName"),
                    site.get("siteLatitude"),
                    site.get("siteLongitude"),
                    site.get("trackerProductType"),
                ),
            )

        tables = cdata.get("tableDetails", [])
        for t in tables:
            sundat = t.get("sundatTablePosition", {})
            cur.execute(
                """
                INSERT INTO dim_tracker
                    (project_id, table_name, x_coord, y_coord, latitude, longitude,
                     module_wattage, module_name, table_type, string_size, string_qty,
                     top_right_lat, top_right_lng, bottom_left_lat, bottom_left_lng,
                     sundat_x, sundat_y, sundat_z)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (project_id, table_name) DO UPDATE SET
                    x_coord         = EXCLUDED.x_coord,
                    y_coord         = EXCLUDED.y_coord,
                    latitude        = EXCLUDED.latitude,
                    longitude       = EXCLUDED.longitude,
                    module_wattage  = EXCLUDED.module_wattage,
                    module_name     = EXCLUDED.module_name,
                    table_type      = EXCLUDED.table_type,
                    string_size     = EXCLUDED.string_size,
                    string_qty      = EXCLUDED.string_qty,
                    top_right_lat   = EXCLUDED.top_right_lat,
                    top_right_lng   = EXCLUDED.top_right_lng,
                    bottom_left_lat = EXCLUDED.bottom_left_lat,
                    bottom_left_lng = EXCLUDED.bottom_left_lng,
                    sundat_x        = EXCLUDED.sundat_x,
                    sundat_y        = EXCLUDED.sundat_y,
                    sundat_z        = EXCLUDED.sundat_z
                """,
                (
                    project_id,
                    t.get("tableName"),
                    _num(t.get("xCoord")), _num(t.get("yCoord")),
                    _num(t.get("latitude")), _num(t.get("longitude")),
                    _int(t.get("moduleWattage")), t.get("moduleName"),
                    t.get("tableType"), _int(t.get("stringSize")), _int(t.get("stringQty")),
                    _num(t.get("TopRightLatitude")), _num(t.get("TopRightLongitude")),
                    _num(t.get("BottomLeftLatitude")), _num(t.get("BottomLeftLongitude")),
                    _num(sundat.get("x")), _num(sundat.get("y")), _num(sundat.get("z")),
                ),
            )
        print(f"    dim_tracker: {len(tables)} rows")
    conn.commit()

    # ── c-f. Flight folders ───────────────────────────────────────────────────
    flight_count = 0
    status_count = 0
    run_count = 0

    for folder in sorted(os.listdir(project_dir)):
        folder_path = os.path.join(project_dir, folder)
        if not os.path.isdir(folder_path) or folder.startswith("_"):
            continue
        flight_date, folder_zones = _parse_folder(folder)
        if not flight_date:
            continue

        subdir_zones = _discover_zone_subdirs(folder_path)
        combined = sorted({_normalize_zone_code(z)
                           for z in set(folder_zones) | set(subdir_zones)})
        if not combined:
            combined = sorted({_normalize_zone_code(z)
                               for z in _discover_tracker_zones(folder_path)})
        if not combined:
            continue

        # Upsert zones
        for zc in combined:
            cur.execute(
                """
                INSERT INTO dim_zone (project_id, zone_code, zone_label)
                VALUES (%s,%s,%s)
                ON CONFLICT (project_id, zone_code) DO NOTHING
                """,
                (project_id, zc, f"Zone {zc}"),
            )

        # Upsert flight
        cur.execute(
            """
            INSERT INTO fact_flight (project_id, flight_date, folder_name, flight_label)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT (project_id, folder_name) DO UPDATE SET
                flight_date = EXCLUDED.flight_date,
                flight_label = EXCLUDED.flight_label
            RETURNING flight_id
            """,
            (project_id, flight_date, folder, folder),
        )
        flight_id = cur.fetchone()[0]
        flight_count += 1

        # Upsert flight-zone links
        for zc in combined:
            cur.execute("SELECT zone_id FROM dim_zone WHERE project_id=%s AND zone_code=%s",
                        (project_id, zc))
            zrow = cur.fetchone()
            if zrow:
                cur.execute(
                    "INSERT INTO fact_flight_zone (flight_id, zone_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                    (flight_id, zrow[0]),
                )

        # Ingest tracker status CSVs
        # Two-pass strategy: regular entries first (DO UPDATE), spine entries second
        # (DO NOTHING) so regular data always wins over spine data when both exist.
        for csv_path in sorted(glob.glob(
                os.path.join(folder_path, "**", "tracker_status*.csv"), recursive=True)):
            rel = os.path.relpath(csv_path, folder_path)
            parts = rel.replace("\\", "/").split("/")
            zone_code = None
            for p in parts:
                m = re.match(r"[Zz]one_(\d+)", p)
                if m:
                    zone_code = _normalize_zone_code(m.group(1))
                    break
                m = re.match(r"[Gg](\d+)", p)
                if m:
                    zone_code = _normalize_zone_code(m.group(1))
                    break
            if not zone_code:
                continue
            cur.execute("SELECT zone_id FROM dim_zone WHERE project_id=%s AND zone_code=%s",
                        (project_id, zone_code))
            zrow = cur.fetchone()
            if not zrow:
                continue
            zone_id = zrow[0]
            try:
                with open(csv_path, "r", encoding="utf-8") as f:
                    all_rows = list(csv.DictReader(f))

                def _upsert_tracker_row(tname, row_data, on_conflict_update):
                    statuses = {col: row_data.get(col) for col in INSTALL_COLS}
                    stage, status = _compute_current_stage(statuses)
                    conflict_clause = (
                        """DO UPDATE SET
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
                            current_status                   = EXCLUDED.current_status"""
                        if on_conflict_update else "DO NOTHING"
                    )
                    cur.execute(
                        f"""
                        INSERT INTO fact_tracker_status
                            (flight_id, zone_id, tracker_name, source_format,
                             pile_installation, lower_journal_installation,
                             slew_drive_installation, torque_tube_installation,
                             torque_tube_coupler_installation, upper_journal_installation,
                             module_rail_installation, pony_panel_installation,
                             solar_module_installation, current_stage, current_status)
                        VALUES (%s,%s,%s,'csv',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (flight_id, zone_id, tracker_name) {conflict_clause}
                        """,
                        (
                            flight_id, zone_id, tname,
                            _normalize_status(statuses.get("pile_installation")),
                            _normalize_status(statuses.get("lower_journal_installation")),
                            _normalize_status(statuses.get("slew_drive_installation")),
                            _normalize_status(statuses.get("torque_tube_installation")),
                            _normalize_status(statuses.get("torque_tube_coupler_installation")),
                            _normalize_status(statuses.get("upper_journal_installation")),
                            _normalize_status(statuses.get("module_rail_installation")),
                            _normalize_status(statuses.get("pony_panel_installation")),
                            _normalize_status(statuses.get("solar_module_installation")),
                            stage, status,
                        ),
                    )

                # Pass 1: regular entries (no suffix) — can overwrite
                for row in all_rows:
                    tname = row.get("tracker_id", "").strip()
                    if not tname:
                        continue
                    tl = tname.lower()
                    if tl.endswith("_boundary_spine") or tl.endswith("_boundary"):
                        continue
                    _upsert_tracker_row(tname, row, on_conflict_update=True)
                    status_count += 1

                # Pass 2: spine/boundary entries (stripped name) — never overwrite regular
                for row in all_rows:
                    tname = row.get("tracker_id", "").strip()
                    if not tname:
                        continue
                    tl = tname.lower()
                    if tl.endswith("_boundary_spine"):
                        canonical = tname[:-15]
                    elif tl.endswith("_boundary"):
                        canonical = tname[:-9]
                    else:
                        continue  # already handled in pass 1
                    if not canonical:
                        continue
                    _upsert_tracker_row(canonical, row, on_conflict_update=False)
                    status_count += 1

            except (OSError, csv.Error):
                pass

        # Ingest tracker status JSONs (per-seed + zone_status.json)
        for root, _dirs, files in os.walk(folder_path):
            rel_root = os.path.relpath(root, folder_path)
            parts = rel_root.replace("\\", "/").split("/")
            zone_code = None
            for p in parts:
                m = re.match(r"[Zz]one_(\d+)", p)
                if m:
                    zone_code = _normalize_zone_code(m.group(1))
                    break
            if not zone_code:
                continue
            cur.execute("SELECT zone_id FROM dim_zone WHERE project_id=%s AND zone_code=%s",
                        (project_id, zone_code))
            zrow = cur.fetchone()
            if not zrow:
                continue
            zone_id = zrow[0]

            # Per-seed JSON files
            ts_dir = os.path.join(root, "tracker_status")
            if os.path.isdir(ts_dir):
                for fname in sorted(os.listdir(ts_dir)):
                    if not fname.endswith(".json"):
                        continue
                    try:
                        with open(os.path.join(ts_dir, fname), "r", encoding="utf-8") as f:
                            data = json.load(f)
                    except (OSError, json.JSONDecodeError):
                        continue
                    seed = data.get("seed_tracker", "")
                    pano = data.get("panorama", {})
                    canvas = pano.get("canvas_size", [None, None])
                    timings = data.get("timings_s", {})
                    cur.execute(
                        """
                        INSERT INTO fact_pipeline_run
                            (flight_id, zone_id, seed_tracker_name,
                             canvas_width, canvas_height, num_source_images,
                             total_detections, source_images,
                             timing_image_selection_s, timing_stitch_and_axes_s,
                             timing_detection_s, timing_assignment_and_bbox_s,
                             timing_status_calculation_s)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (flight_id, zone_id, seed_tracker_name) DO NOTHING
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
                    cur.execute(
                        "SELECT run_id FROM fact_pipeline_run "
                        "WHERE flight_id=%s AND zone_id=%s AND seed_tracker_name=%s",
                        (flight_id, zone_id, seed),
                    )
                    run_id_row = cur.fetchone()
                    run_id = run_id_row[0] if run_id_row else None
                    run_count += 1

                    for tracker in data.get("trackers", []):
                        tname = tracker.get("tracker_name", "").strip()
                        if not tname:
                            continue
                        tl = tname.lower()
                        if tl.endswith("_boundary_spine"):
                            tname = tname[:-15]
                        elif tl.endswith("_boundary"):
                            tname = tname[:-9]
                        if not tname:
                            continue
                        statuses = tracker.get("construction_status", {})
                        stage, status = _compute_current_stage(statuses)
                        cur.execute(
                            """
                            INSERT INTO fact_tracker_status
                                (flight_id, zone_id, pipeline_run_id, tracker_name, source_format,
                                 pile_installation, lower_journal_installation,
                                 slew_drive_installation, torque_tube_installation,
                                 torque_tube_coupler_installation, upper_journal_installation,
                                 module_rail_installation, pony_panel_installation,
                                 solar_module_installation, current_stage, current_status)
                            VALUES (%s,%s,%s,%s,'json',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (flight_id, zone_id, tracker_name) DO UPDATE SET
                                pipeline_run_id                  = COALESCE(EXCLUDED.pipeline_run_id, fact_tracker_status.pipeline_run_id),
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
                                flight_id, zone_id, run_id, tname,
                                _normalize_status(statuses.get("pile_installation")),
                                _normalize_status(statuses.get("lower_journal_installation")),
                                _normalize_status(statuses.get("slew_drive_installation")),
                                _normalize_status(statuses.get("torque_tube_installation")),
                                _normalize_status(statuses.get("torque_tube_coupler_installation")),
                                _normalize_status(statuses.get("upper_journal_installation")),
                                _normalize_status(statuses.get("module_rail_installation")),
                                _normalize_status(statuses.get("pony_panel_installation")),
                                _normalize_status(statuses.get("solar_module_installation")),
                                stage, status,
                            ),
                        )
                        status_count += 1

            # zone_status.json
            zs_path = os.path.join(root, "zone_status.json")
            if os.path.isfile(zs_path):
                try:
                    with open(zs_path, "r", encoding="utf-8") as f:
                        zdata = json.load(f)
                except (OSError, json.JSONDecodeError):
                    continue
                trackers = zdata.get("trackers", {})
                items = (list(trackers.items()) if isinstance(trackers, dict)
                         else [(t.get("tracker_name", t.get("tracker_id", "")), t)
                               for t in trackers] if isinstance(trackers, list)
                         else [])
                for tname, tinfo in items:
                    tname = tname.strip() if isinstance(tname, str) else str(tname)
                    if not tname:
                        continue
                    tl = tname.lower()
                    if tl.endswith("_boundary_spine"):
                        tname = tname[:-15]
                    elif tl.endswith("_boundary"):
                        tname = tname[:-9]
                    if not tname:
                        continue
                    statuses = tinfo if isinstance(tinfo, dict) else {}
                    if "construction_status" in statuses:
                        statuses = statuses["construction_status"]
                    stage, status = _compute_current_stage(statuses)
                    cur.execute(
                        """
                        INSERT INTO fact_tracker_status
                            (flight_id, zone_id, tracker_name, source_format,
                             pile_installation, lower_journal_installation,
                             slew_drive_installation, torque_tube_installation,
                             torque_tube_coupler_installation, upper_journal_installation,
                             module_rail_installation, pony_panel_installation,
                             solar_module_installation, current_stage, current_status)
                        VALUES (%s,%s,%s,'json',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (flight_id, zone_id, tracker_name) DO UPDATE SET
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
                            flight_id, zone_id, tname,
                            _normalize_status(statuses.get("pile_installation")),
                            _normalize_status(statuses.get("lower_journal_installation")),
                            _normalize_status(statuses.get("slew_drive_installation")),
                            _normalize_status(statuses.get("torque_tube_installation")),
                            _normalize_status(statuses.get("torque_tube_coupler_installation")),
                            _normalize_status(statuses.get("upper_journal_installation")),
                            _normalize_status(statuses.get("module_rail_installation")),
                            _normalize_status(statuses.get("pony_panel_installation")),
                            _normalize_status(statuses.get("solar_module_installation")),
                            stage, status,
                        ),
                    )
                    status_count += 1

        conn.commit()

    print(f"    fact_flight: {flight_count} folders ingested")
    print(f"    fact_tracker_status: {status_count} rows")
    print(f"    fact_pipeline_run: {run_count} rows")

    # ── e. fact_manpower_daily + fact_stage_milestone ─────────────────────────
    mp_path = os.path.join(project_dir, "_app_data", "manpower_data.json")
    if os.path.isfile(mp_path):
        with open(mp_path, "r", encoding="utf-8") as f:
            mp = json.load(f)
        manual_dates = set(mp.get("manual_dates", []))
        mp_count = 0
        for date_str, counts in mp.get("manpower_config", {}).items():
            cur.execute(
                """
                INSERT INTO fact_manpower_daily
                    (project_id, work_date, pile_workers, torque_tube_workers,
                     module_rails_workers, solar_panel_workers, total_workers, is_manual_date)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (project_id, work_date) DO UPDATE SET
                    pile_workers         = EXCLUDED.pile_workers,
                    torque_tube_workers  = EXCLUDED.torque_tube_workers,
                    module_rails_workers = EXCLUDED.module_rails_workers,
                    solar_panel_workers  = EXCLUDED.solar_panel_workers,
                    total_workers        = EXCLUDED.total_workers,
                    is_manual_date       = EXCLUDED.is_manual_date
                """,
                (
                    project_id, date_str,
                    counts.get("pile", 0), counts.get("torque_tube", 0),
                    counts.get("module_rails", 0), counts.get("solar_panel", 0),
                    counts.get("total", 0), date_str in manual_dates,
                ),
            )
            mp_count += 1

        ms_count = 0
        for stage_key, date_val in mp.get("actual_stage_dates", {}).items():
            stage_id = STAGE_KEY_TO_ID.get(stage_key)
            if stage_id is None:
                continue
            cur.execute(
                """
                INSERT INTO fact_stage_milestone (project_id, stage_id, actual_completion_date)
                VALUES (%s,%s,%s)
                ON CONFLICT (project_id, stage_id) DO UPDATE SET
                    actual_completion_date = EXCLUDED.actual_completion_date
                """,
                (project_id, stage_id, date_val),
            )
            ms_count += 1

        conn.commit()
        print(f"    fact_manpower_daily: {mp_count} rows")
        print(f"    fact_stage_milestone: {ms_count} rows")

    # ── f. fact_tracker_extraction ────────────────────────────────────────────
    crop_files = glob.glob(
        os.path.join(project_dir, "**", "extracted_tracker_images",
                     "target_plus_neighbors_crops.json"),
        recursive=True,
    )
    ext_count = 0
    for crop_path in sorted(crop_files):
        rel = os.path.relpath(crop_path, project_dir)
        parts = rel.replace("\\", "/").split("/")
        folder_name = parts[0]
        zone_code = None
        for p in parts:
            m = re.match(r"[Zz]one_(\d+)", p)
            if m:
                zone_code = _normalize_zone_code(m.group(1))
                break
        if not zone_code:
            continue
        cur.execute(
            "SELECT flight_id FROM fact_flight WHERE project_id=%s AND folder_name=%s",
            (project_id, folder_name),
        )
        frow = cur.fetchone()
        cur.execute(
            "SELECT zone_id FROM dim_zone WHERE project_id=%s AND zone_code=%s",
            (project_id, zone_code),
        )
        zrow = cur.fetchone()
        if not frow or not zrow:
            continue
        flight_id, zone_id = frow[0], zrow[0]
        rank_path = os.path.join(os.path.dirname(crop_path), "tracker_best_rank_index.json")
        rank_map = {}
        if os.path.isfile(rank_path):
            try:
                with open(rank_path, "r", encoding="utf-8") as f:
                    rank_map = json.load(f)
            except (OSError, json.JSONDecodeError):
                pass
        try:
            with open(crop_path, "r", encoding="utf-8") as f:
                crops_data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        for tracker_name, detail in crops_data.get("trackers", {}).items():
            summary = detail.get("summary", {})
            cur.execute(
                """
                INSERT INTO fact_tracker_extraction
                    (flight_id, zone_id, tracker_name, best_rank_index,
                     saved_count, rejected_count, total_processed, extraction_detail)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (flight_id, zone_id, tracker_name) DO UPDATE SET
                    best_rank_index   = EXCLUDED.best_rank_index,
                    saved_count       = EXCLUDED.saved_count,
                    rejected_count    = EXCLUDED.rejected_count,
                    total_processed   = EXCLUDED.total_processed,
                    extraction_detail = EXCLUDED.extraction_detail
                """,
                (
                    flight_id, zone_id, tracker_name,
                    rank_map.get(tracker_name),
                    summary.get("saved_count"),
                    summary.get("rejected_count"),
                    summary.get("total_processed"),
                    json.dumps(detail),
                ),
            )
            ext_count += 1
        conn.commit()

    if ext_count:
        print(f"    fact_tracker_extraction: {ext_count} rows")


# ── Verification ─────────────────────────────────────────────────────────────

def verify(conn):
    print(f"\n{'─'*60}")
    print("  Verification — row counts")
    print(f"{'─'*60}")
    tables = [
        "dim_project", "dim_site", "dim_zone", "dim_tracker",
        "dim_stage", "dim_installation_step",
        "fact_flight", "fact_flight_zone", "fact_pipeline_run",
        "fact_tracker_status", "fact_manpower_daily",
        "fact_stage_milestone", "fact_tracker_extraction",
        "fact_processing_timing",
    ]
    cur = conn.cursor()
    for t in tables:
        cur.execute(f"SELECT COUNT(*) FROM {t}")
        n = cur.fetchone()[0]
        print(f"    {t:<35} {n:>6} rows")
    cur.close()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    start = time.time()
    print("=" * 60)
    print("  DRONE PROGRESS — FULL DATABASE RESET & SEED")
    print("=" * 60)

    cfg = _load_config()

    # 1. Drop + recreate DB
    reset_database(cfg)

    # 2-4. Schema + seed
    conn = _app_conn(cfg)
    print(f"\n{'─'*60}")
    print("  Steps 2-4 — Schema and reference data")
    print(f"{'─'*60}")
    apply_sql_file(conn, INIT_SQL, "init.sql (extensions + roles)")
    apply_sql_file(conn, SCHEMA_SQL, "schema.sql (tables + indexes)")
    apply_sql_file(conn, SEED_SQL, "seed_reference.sql (stages + steps)")

    # 5. Seed project data
    print(f"\n{'─'*60}")
    print("  Step 5 — Seeding project data from layout_data/")
    print(f"{'─'*60}")
    cur = conn.cursor()
    for project in sorted(os.listdir(LAYOUT_DIR)):
        project_dir = os.path.join(LAYOUT_DIR, project)
        if not os.path.isdir(project_dir) or project.startswith("_"):
            continue
        seed_project(cur, conn, project, project_dir)
    cur.close()

    # 6. Verify
    verify(conn)
    conn.close()

    elapsed = time.time() - start
    print(f"\n{'═'*60}")
    print(f"  Setup complete in {elapsed:.1f}s")
    print(f"{'═'*60}")


if __name__ == "__main__":
    main()
