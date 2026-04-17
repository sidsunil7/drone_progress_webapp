"""
Data-access layer for the Flask application.

All SQL queries live here, organised by domain.
Functions return plain Python dicts / lists — no DB objects leak out.
"""

import logging
from datetime import datetime, timezone

from db.db_service import query, query_one, execute, execute_returning, execute_transaction

logger = logging.getLogger(__name__)

PRODUCTIVITY_STAGE_KEYS = ["pile", "torque_tube", "module_rails", "solar_panel"]

STAGE_KEY_TO_ID = {
    "pile": 1,
    "torque_tube": 2,
    "module_rails": 3,
    "solar_panel": 4,
}

STAGE_ID_TO_KEY = {v: k for k, v in STAGE_KEY_TO_ID.items()}


def _utc_now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─────────────────────────────────────────────────────────────────────────────
# ProjectRepo
# ─────────────────────────────────────────────────────────────────────────────

class ProjectRepo:

    @staticmethod
    def list_projects() -> list[str]:
        """Return sorted list of project names."""
        rows = query("SELECT project_name FROM dim_project ORDER BY project_name")
        return [r["project_name"] for r in rows]

    @staticmethod
    def get_project_id(project_name: str):
        """Return UUID for a project, or None."""
        row = query_one(
            "SELECT project_id FROM dim_project WHERE project_name = %s",
            (project_name,),
        )
        return row["project_id"] if row else None

    @staticmethod
    def get_settings(project_name: str) -> dict | None:
        """Return dim_project row with total_project_trackers appended."""
        row = query_one(
            """
            SELECT p.project_id, p.project_name, p.start_date, p.end_date,
                   p.hours_per_day, p.modules_per_tracker, p.mw_per_tracker,
                   p.working_days, p.created_at, p.updated_at,
                   COUNT(t.tracker_id)::int AS total_project_trackers,
                   MIN(t.module_wattage)    AS module_wattage,
                   MIN(t.string_size)       AS string_size,
                   MIN(t.string_qty)        AS string_qty
            FROM dim_project p
            LEFT JOIN dim_tracker t USING (project_id)
            WHERE p.project_name = %s
            GROUP BY p.project_id
            """,
            (project_name,),
        )
        return row

    @staticmethod
    def save_settings(project_name: str, payload: dict) -> dict | None:
        """Upsert project settings. Returns the updated row."""
        now = _utc_now_iso()
        execute(
            """
            UPDATE dim_project SET
                start_date          = %s,
                end_date            = %s,
                hours_per_day       = %s,
                modules_per_tracker = %s,
                mw_per_tracker      = %s,
                working_days        = %s,
                updated_at          = %s
            WHERE project_name = %s
            """,
            (
                payload.get("project_start_date"),
                payload.get("project_end_date"),
                payload.get("hours_per_day"),
                payload.get("modules_per_tracker"),
                payload.get("mw_per_tracker"),
                payload.get("working_days"),
                now,
                project_name,
            ),
        )
        return ProjectRepo.get_settings(project_name)

    @staticmethod
    def create_project(project_name: str, payload: dict) -> dict | None:
        """Insert a new project row. Returns the created row."""
        now = _utc_now_iso()
        execute(
            """
            INSERT INTO dim_project
                (project_name, start_date, end_date, hours_per_day,
                 modules_per_tracker, mw_per_tracker, working_days,
                 created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (project_name) DO NOTHING
            """,
            (
                project_name,
                payload.get("project_start_date"),
                payload.get("project_end_date"),
                payload.get("hours_per_day"),
                payload.get("modules_per_tracker"),
                payload.get("mw_per_tracker"),
                payload.get("working_days"),
                now,
                now,
            ),
        )
        return ProjectRepo.get_settings(project_name)


# ─────────────────────────────────────────────────────────────────────────────
# ManpowerRepo
# ─────────────────────────────────────────────────────────────────────────────

class ManpowerRepo:

    @staticmethod
    def get_manpower(project_name: str) -> dict:
        """
        Return the same shape as the old manpower_data.json:
          {
            project_name, manual_dates, manpower_config,
            actual_stage_dates, created_at, updated_at, data_exists
          }
        """
        project_id = ProjectRepo.get_project_id(project_name)
        if not project_id:
            return {
                "project_name": project_name,
                "manual_dates": [],
                "manpower_config": {},
                "actual_stage_dates": {k: None for k in PRODUCTIVITY_STAGE_KEYS},
                "created_at": None,
                "updated_at": None,
                "data_exists": False,
            }

        rows = query(
            """
            SELECT work_date, pile_workers, torque_tube_workers,
                   module_rails_workers, solar_panel_workers, total_workers,
                   is_manual_date, created_at
            FROM fact_manpower_daily
            WHERE project_id = %s
            ORDER BY work_date
            """,
            (project_id,),
        )

        milestones = query(
            """
            SELECT s.stage_key, m.actual_completion_date
            FROM fact_stage_milestone m
            JOIN dim_stage s ON s.stage_id = m.stage_id
            WHERE m.project_id = %s
            """,
            (project_id,),
        )

        manpower_config = {}
        manual_dates = []
        created_at = None
        updated_at = None

        for row in rows:
            date_str = str(row["work_date"])
            manpower_config[date_str] = {
                "pile": row["pile_workers"],
                "torque_tube": row["torque_tube_workers"],
                "module_rails": row["module_rails_workers"],
                "solar_panel": row["solar_panel_workers"],
                "total": row["total_workers"],
            }
            if row["is_manual_date"]:
                manual_dates.append(date_str)
            if created_at is None and row["created_at"]:
                created_at = row["created_at"].strftime("%Y-%m-%dT%H:%M:%SZ")

        actual_stage_dates = {k: None for k in PRODUCTIVITY_STAGE_KEYS}
        for m in milestones:
            key = m["stage_key"]
            val = m["actual_completion_date"]
            actual_stage_dates[key] = str(val) if val else None
            if val and updated_at is None:
                updated_at = _utc_now_iso()

        return {
            "project_name": project_name,
            "manual_dates": sorted(manual_dates),
            "manpower_config": manpower_config,
            "actual_stage_dates": actual_stage_dates,
            "created_at": created_at,
            "updated_at": updated_at,
            "data_exists": bool(rows),
        }

    @staticmethod
    def save_manpower(project_name: str, payload: dict):
        """
        Upsert fact_manpower_daily and fact_stage_milestone from payload:
          { manual_dates, manpower_config, actual_stage_dates }
        """
        project_id = ProjectRepo.get_project_id(project_name)
        if not project_id:
            raise FileNotFoundError(f"Project not found: {project_name}")

        manual_dates_set = set(payload.get("manual_dates", []))
        manpower_config = payload.get("manpower_config") or {}
        actual_stage_dates = payload.get("actual_stage_dates", {})

        statements = []

        # Remove rows no longer present in the payload (e.g. manual date removed in UI).
        # Only run when the client sends at least one date, so an empty payload cannot wipe the table.
        if manpower_config:
            keep_dates = sorted(manpower_config.keys())
            placeholders = ",".join(["%s"] * len(keep_dates))
            statements.append(
                (
                    f"""
                    DELETE FROM fact_manpower_daily
                    WHERE project_id = %s
                      AND work_date NOT IN ({placeholders})
                    """,
                    (project_id, *keep_dates),
                )
            )

        for date_str, counts in manpower_config.items():
            statements.append((
                """
                INSERT INTO fact_manpower_daily
                    (project_id, work_date, pile_workers, torque_tube_workers,
                     module_rails_workers, solar_panel_workers, total_workers, is_manual_date)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (project_id, work_date) DO UPDATE SET
                    pile_workers         = EXCLUDED.pile_workers,
                    torque_tube_workers  = EXCLUDED.torque_tube_workers,
                    module_rails_workers = EXCLUDED.module_rails_workers,
                    solar_panel_workers  = EXCLUDED.solar_panel_workers,
                    total_workers        = EXCLUDED.total_workers,
                    is_manual_date       = EXCLUDED.is_manual_date
                """,
                (
                    project_id,
                    date_str,
                    counts.get("pile", 0),
                    counts.get("torque_tube", 0),
                    counts.get("module_rails", 0),
                    counts.get("solar_panel", 0),
                    counts.get("total", 0),
                    date_str in manual_dates_set,
                ),
            ))

        for stage_key, date_val in actual_stage_dates.items():
            stage_id = STAGE_KEY_TO_ID.get(stage_key)
            if stage_id is None:
                continue
            statements.append((
                """
                INSERT INTO fact_stage_milestone (project_id, stage_id, actual_completion_date)
                VALUES (%s, %s, %s)
                ON CONFLICT (project_id, stage_id) DO UPDATE SET
                    actual_completion_date = EXCLUDED.actual_completion_date
                """,
                (project_id, stage_id, date_val),
            ))

        if statements:
            execute_transaction(statements)

        return ManpowerRepo.get_manpower(project_name)


# ─────────────────────────────────────────────────────────────────────────────
# TrackerRepo
# ─────────────────────────────────────────────────────────────────────────────

class TrackerRepo:

    @staticmethod
    def get_tracker_defaults(project_name: str) -> dict:
        """Return mw_per_tracker, modules_per_tracker, total_project_trackers."""
        row = query_one(
            """
            SELECT p.mw_per_tracker, p.modules_per_tracker,
                   COUNT(t.tracker_id)::int AS total_project_trackers
            FROM dim_project p
            LEFT JOIN dim_tracker t USING (project_id)
            WHERE p.project_name = %s
            GROUP BY p.project_id
            """,
            (project_name,),
        )
        if not row:
            return {
                "mw_per_tracker": None,
                "modules_per_tracker": None,
                "total_project_trackers": None,
                "project_json_path": None,
            }
        return {
            "mw_per_tracker": float(row["mw_per_tracker"]) if row["mw_per_tracker"] is not None else None,
            "modules_per_tracker": row["modules_per_tracker"],
            "total_project_trackers": row["total_project_trackers"] or None,
            "project_json_path": None,
        }

    @staticmethod
    def get_boundaries(project_name: str) -> dict:
        """
        Return tracker boundaries as:
          { table_name: { min_lon, max_lon, min_lat, max_lat } }
        """
        rows = query(
            """
            SELECT t.table_name,
                   t.top_right_lat, t.top_right_lng,
                   t.bottom_left_lat, t.bottom_left_lng
            FROM dim_tracker t
            JOIN dim_project p USING (project_id)
            WHERE p.project_name = %s
              AND t.top_right_lat IS NOT NULL
            """,
            (project_name,),
        )
        boundaries = {}
        for row in rows:
            tr_lat = float(row["top_right_lat"])
            tr_lon = float(row["top_right_lng"])
            bl_lat = float(row["bottom_left_lat"])
            bl_lon = float(row["bottom_left_lng"])
            boundaries[row["table_name"]] = {
                "min_lon": min(bl_lon, tr_lon),
                "max_lon": max(bl_lon, tr_lon),
                "min_lat": min(bl_lat, tr_lat),
                "max_lat": max(bl_lat, tr_lat),
            }
        return boundaries

    @staticmethod
    def get_zone_bounds(project_name: str) -> dict:
        """
        Return zone bounding boxes keyed by zone_code (e.g. 'G01', 'G42'):
          { zone_code: [min_lat, min_lon, max_lat, max_lon] }

        Derives zone from the tracker table_name prefix (e.g. 'A01T...' → 'G01')
        so ALL zones with tracker data are returned, not just those that have
        had a drone flight (dim_zone).  This ensures the block-map overlay
        covers the full satellite image extent.
        """
        rows = query(
            """
            SELECT
                'G' || LPAD(LTRIM(SUBSTRING(t.table_name FROM 2 FOR 2), '0'), 2, '0') AS zone_code,
                MIN(LEAST(t.bottom_left_lat, t.top_right_lat))    AS min_lat,
                MIN(LEAST(t.bottom_left_lng, t.top_right_lng))    AS min_lon,
                MAX(GREATEST(t.bottom_left_lat, t.top_right_lat)) AS max_lat,
                MAX(GREATEST(t.bottom_left_lng, t.top_right_lng)) AS max_lon
            FROM dim_tracker t
            JOIN dim_project p ON p.project_id = t.project_id
            WHERE p.project_name = %s
              AND t.top_right_lat IS NOT NULL
              AND t.top_right_lng IS NOT NULL
              AND t.bottom_left_lat IS NOT NULL
              AND t.bottom_left_lng IS NOT NULL
            GROUP BY zone_code
            ORDER BY zone_code
            """,
            (project_name,),
        )

        zone_bounds = {}
        for row in rows:
            if row["min_lat"] is not None:
                zone_bounds[row["zone_code"]] = [
                    float(row["min_lat"]),
                    float(row["min_lon"]),
                    float(row["max_lat"]),
                    float(row["max_lon"]),
                ]
        return zone_bounds


# ─────────────────────────────────────────────────────────────────────────────
# FlightRepo
# ─────────────────────────────────────────────────────────────────────────────

class FlightRepo:

    @staticmethod
    def has_zones(project_name: str) -> bool:
        """True if the project has any zone records and tracker data."""
        row = query_one(
            """
            SELECT EXISTS(
                SELECT 1 FROM dim_zone z
                JOIN dim_project p USING (project_id)
                WHERE p.project_name = %s
            ) AS has_zones
            """,
            (project_name,),
        )
        return bool(row and row["has_zones"])

    @staticmethod
    def get_all_dates(project_name: str) -> list[dict]:
        """
        Return list of { date, folder, display } dicts sorted by date, deduplicated
        by date (one entry per calendar day, matching old dir-scan behaviour).
        """
        rows = query(
            """
            SELECT DISTINCT ON (flight_date)
                   flight_date, folder_name
            FROM fact_flight f
            JOIN dim_project p USING (project_id)
            WHERE p.project_name = %s
            ORDER BY flight_date, folder_name
            """,
            (project_name,),
        )
        result = []
        for row in rows:
            d = row["flight_date"]
            date_str = d.strftime("%Y%m%d") if hasattr(d, "strftime") else str(d).replace("-", "")
            result.append({
                "date": date_str,
                "folder": row["folder_name"],
                "display": d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d),
            })
        return result

    @staticmethod
    def get_available_zones(project_name: str) -> list[str]:
        """Return sorted list of zone codes for a project."""
        rows = query(
            """
            SELECT z.zone_code
            FROM dim_zone z
            JOIN dim_project p USING (project_id)
            WHERE p.project_name = %s
            ORDER BY z.zone_code
            """,
            (project_name,),
        )
        return [r["zone_code"] for r in rows]

    @staticmethod
    def get_zone_dates(project_name: str, zone_code: str) -> list[dict]:
        """Return { date, folder, display } list for a specific zone."""
        rows = query(
            """
            SELECT f.folder_name, f.flight_date
            FROM fact_flight f
            JOIN dim_project p ON p.project_id = f.project_id
            JOIN fact_flight_zone fz ON fz.flight_id = f.flight_id
            JOIN dim_zone z ON z.zone_id = fz.zone_id
            WHERE p.project_name = %s
              AND z.zone_code = %s
            ORDER BY f.flight_date
            """,
            (project_name, zone_code),
        )
        result = []
        for row in rows:
            d = row["flight_date"]
            date_str = d.strftime("%Y%m%d") if hasattr(d, "strftime") else str(d).replace("-", "")
            result.append({
                "date": date_str,
                "folder": row["folder_name"],
                "display": d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d),
            })
        return result

    @staticmethod
    def get_flight_folder(project_name: str, folder_name: str) -> str | None:
        """Return folder_name from DB (used to locate images on disk)."""
        row = query_one(
            """
            SELECT f.folder_name
            FROM fact_flight f
            JOIN dim_project p USING (project_id)
            WHERE p.project_name = %s AND f.folder_name = %s
            """,
            (project_name, folder_name),
        )
        return row["folder_name"] if row else None

    @staticmethod
    def get_all_zone_available_dates(project_name: str) -> dict:
        """
        Return { zone_code: [(date_id, folder_name), ...] } sorted ascending.
        Mirrors _all_zone_available_dates_cached behaviour.
        """
        rows = query(
            """
            SELECT z.zone_code, f.folder_name, f.flight_date
            FROM fact_flight f
            JOIN dim_project p ON p.project_id = f.project_id
            JOIN fact_flight_zone fz ON fz.flight_id = f.flight_id
            JOIN dim_zone z ON z.zone_id = fz.zone_id
            WHERE p.project_name = %s
            ORDER BY z.zone_code, f.flight_date
            """,
            (project_name,),
        )
        result: dict[str, list] = {}
        for row in rows:
            d = row["flight_date"]
            date_id = d.strftime("%Y%m%d") if hasattr(d, "strftime") else str(d).replace("-", "")
            zc = row["zone_code"]
            if zc not in result:
                result[zc] = []
            result[zc].append((date_id, row["folder_name"]))
        return result

    @staticmethod
    def get_latest_flight_for_zone(project_name: str, zone_code: str, date_id: str):
        """
        Return (date_id, folder_name) for the most recent flight covering zone
        at or before date_id (YYYYMMDD string). Returns None if none found.
        """
        row = query_one(
            """
            SELECT f.folder_name, f.flight_date
            FROM fact_flight f
            JOIN dim_project p ON p.project_id = f.project_id
            JOIN fact_flight_zone fz ON fz.flight_id = f.flight_id
            JOIN dim_zone z ON z.zone_id = fz.zone_id
            WHERE p.project_name = %s
              AND z.zone_code = %s
              AND TO_CHAR(f.flight_date, 'YYYYMMDD') <= %s
            ORDER BY f.flight_date DESC
            LIMIT 1
            """,
            (project_name, zone_code, date_id),
        )
        if not row:
            return None
        d = row["flight_date"]
        actual_date_id = d.strftime("%Y%m%d") if hasattr(d, "strftime") else str(d).replace("-", "")
        return (actual_date_id, row["folder_name"])

    @staticmethod
    def get_flight_id(project_name: str, folder_name: str):
        """Return the UUID flight_id for a folder_name."""
        row = query_one(
            """
            SELECT f.flight_id
            FROM fact_flight f
            JOIN dim_project p USING (project_id)
            WHERE p.project_name = %s AND f.folder_name = %s
            """,
            (project_name, folder_name),
        )
        return row["flight_id"] if row else None

    @staticmethod
    def get_zone_id(project_name: str, zone_code: str):
        """Return the UUID zone_id for a zone_code."""
        row = query_one(
            """
            SELECT z.zone_id
            FROM dim_zone z
            JOIN dim_project p USING (project_id)
            WHERE p.project_name = %s AND z.zone_code = %s
            """,
            (project_name, zone_code),
        )
        return row["zone_id"] if row else None


# ─────────────────────────────────────────────────────────────────────────────
# TrackerStatusRepo
# ─────────────────────────────────────────────────────────────────────────────

class TrackerStatusRepo:

    @staticmethod
    def get_tracker_info(flight_id, zone_id) -> dict:
        """
        Return tracker_info dict:
          { tracker_name: { stage: str, status: str } }
        Mirrors the shape returned by load_tracker_info / load_tracker_info_json.
        """
        if not flight_id or not zone_id:
            return {}
        rows = query(
            """
            SELECT tracker_name, current_stage, current_status
            FROM fact_tracker_status
            WHERE flight_id = %s AND zone_id = %s
            """,
            (flight_id, zone_id),
        )
        return {
            row["tracker_name"]: {
                "stage": row["current_stage"] or "",
                "status": row["current_status"] or "",
            }
            for row in rows
        }

    @staticmethod
    def get_tracker_info_by_folder_zone(project_name: str, folder_name_or_latest: str, zone_code: str) -> dict:
        """Convenience wrapper: resolve IDs then return tracker info.

        If folder_name_or_latest == 'latest', uses the most recent flight for the zone.
        """
        if folder_name_or_latest == "latest":
            # Return the most-recent stage/status per tracker for this zone.
            # DISTINCT ON keeps only the first row per tracker_name after the
            # ORDER BY, which is the row from the latest flight.
            rows = query(
                """
                SELECT DISTINCT ON (ts.tracker_name)
                       ts.tracker_name, ts.current_stage, ts.current_status
                FROM fact_tracker_status ts
                JOIN fact_flight f ON f.flight_id = ts.flight_id
                JOIN dim_project p ON p.project_id = f.project_id
                JOIN dim_zone z ON z.zone_id = ts.zone_id
                WHERE p.project_name = %s AND z.zone_code = %s
                ORDER BY ts.tracker_name, f.flight_date DESC
                """,
                (project_name, zone_code),
            )
        else:
            flight_id = FlightRepo.get_flight_id(project_name, folder_name_or_latest)
            zone_id = FlightRepo.get_zone_id(project_name, zone_code)
            if not flight_id or not zone_id:
                return {}
            rows = query(
                """
                SELECT tracker_name, current_stage, current_status
                FROM fact_tracker_status
                WHERE flight_id = %s AND zone_id = %s
                """,
                (flight_id, zone_id),
            )
        return {
            row["tracker_name"]: {
                "stage": row["current_stage"] or "",
                "status": row["current_status"] or "",
            }
            for row in rows
        }

    @staticmethod
    def get_date_summary(flight_id, zone_id) -> dict:
        """
        Return the same shape as build_date_summary_cached:
          { stageStatusCounts, totalTrackers, trackerStages }
        Uses a single GROUP BY query instead of loading all rows.
        """
        if not flight_id or not zone_id:
            return {"stageStatusCounts": {}, "totalTrackers": 0, "trackerStages": []}

        agg_rows = query(
            """
            SELECT current_stage, current_status, COUNT(*)::int AS cnt
            FROM fact_tracker_status
            WHERE flight_id = %s AND zone_id = %s
              AND current_stage IS NOT NULL AND current_stage <> ''
            GROUP BY current_stage, current_status
            """,
            (flight_id, zone_id),
        )

        total_row = query_one(
            "SELECT COUNT(*)::int AS total FROM fact_tracker_status WHERE flight_id = %s AND zone_id = %s",
            (flight_id, zone_id),
        )

        stage_status_counts = {}
        tracker_stages = []

        for row in agg_rows:
            stage = row["current_stage"]
            status = row["current_status"] or ""
            cnt = row["cnt"]
            if stage not in stage_status_counts:
                stage_status_counts[stage] = {}
            stage_status_counts[stage][status] = cnt
            for _ in range(cnt):
                tracker_stages.append({"stage": stage, "status": status})

        return {
            "stageStatusCounts": stage_status_counts,
            "totalTrackers": total_row["total"] if total_row else 0,
            "trackerStages": tracker_stages,
        }

    @staticmethod
    def get_zone_majority_stage(flight_id, zone_id) -> str | None:
        """Return the most common current_stage for a flight+zone."""
        row = query_one(
            """
            SELECT current_stage, COUNT(*) AS cnt
            FROM fact_tracker_status
            WHERE flight_id = %s AND zone_id = %s
              AND current_stage IS NOT NULL AND current_stage <> ''
            GROUP BY current_stage
            ORDER BY cnt DESC
            LIMIT 1
            """,
            (flight_id, zone_id),
        )
        return row["current_stage"] if row else None

    @staticmethod
    def get_all_zone_stages(project_name: str, date_id: str) -> dict:
        """
        Return { zone_code: majority_stage } for all zones at exactly date_id.
        Mirrors _all_zone_stages_cached.
        """
        rows = query(
            """
            SELECT z.zone_code, ts.current_stage, COUNT(*)::int AS cnt
            FROM fact_tracker_status ts
            JOIN fact_flight f ON f.flight_id = ts.flight_id
            JOIN dim_project p ON p.project_id = f.project_id
            JOIN dim_zone z ON z.zone_id = ts.zone_id
            WHERE p.project_name = %s
              AND TO_CHAR(f.flight_date, 'YYYYMMDD') = %s
              AND ts.current_stage IS NOT NULL AND ts.current_stage <> ''
            GROUP BY z.zone_code, ts.current_stage
            ORDER BY z.zone_code, cnt DESC
            """,
            (project_name, date_id),
        )
        result = {}
        for row in rows:
            zc = row["zone_code"]
            if zc not in result:
                result[zc] = row["current_stage"]
        return result

    @staticmethod
    def get_all_zone_stages_forward_filled(project_name: str, date_id: str) -> dict:
        """
        Return { zone_code: majority_stage } forward-filled to date_id.
        Each zone picks its most recent flight at or before date_id.
        Mirrors _all_zone_stages_forward_fill_cached.
        """
        rows = query(
            """
            WITH latest_flights AS (
                SELECT DISTINCT ON (z.zone_code)
                       z.zone_code,
                       f.flight_id
                FROM fact_flight f
                JOIN dim_project p ON p.project_id = f.project_id
                JOIN fact_flight_zone fz ON fz.flight_id = f.flight_id
                JOIN dim_zone z ON z.zone_id = fz.zone_id
                WHERE p.project_name = %s
                  AND TO_CHAR(f.flight_date, 'YYYYMMDD') <= %s
                ORDER BY z.zone_code, f.flight_date DESC
            ),
            stage_counts AS (
                SELECT lf.zone_code, ts.current_stage, COUNT(*)::int AS cnt
                FROM latest_flights lf
                JOIN fact_tracker_status ts ON ts.flight_id = lf.flight_id
                JOIN dim_zone z ON z.zone_id = ts.zone_id AND z.zone_code = lf.zone_code
                WHERE ts.current_stage IS NOT NULL AND ts.current_stage <> ''
                GROUP BY lf.zone_code, ts.current_stage
            )
            SELECT DISTINCT ON (zone_code) zone_code, current_stage
            FROM stage_counts
            ORDER BY zone_code, cnt DESC
            """,
            (project_name, date_id),
        )
        return {row["zone_code"]: row["current_stage"] for row in rows}

    @staticmethod
    def get_zone_completion(flight_id, zone_id) -> dict:
        """
        Return { total, completed, pct } for a zone at a given flight.
        completed = trackers at solar_panel + completed.
        """
        total_row = query_one(
            "SELECT COUNT(*)::int AS total FROM fact_tracker_status WHERE flight_id = %s AND zone_id = %s",
            (flight_id, zone_id),
        )
        completed_row = query_one(
            """
            SELECT COUNT(*)::int AS completed
            FROM fact_tracker_status
            WHERE flight_id = %s AND zone_id = %s
              AND current_stage = 'solar_panel'
              AND current_status = 'completed'
            """,
            (flight_id, zone_id),
        )
        total = (total_row["total"] if total_row else 0) or 0
        completed = (completed_row["completed"] if completed_row else 0) or 0
        pct = f"{(completed / total * 100):.1f}" if total > 0 else "0.0"
        return {"total": total, "completed": completed, "pct": pct}
