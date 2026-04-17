-- =============================================================
-- Schema: drone_progress
-- 14 tables (6 dimension + 8 fact)
-- =============================================================

-- ─────────────────────────────────────────────
-- DIMENSION TABLES
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS dim_project (
    project_id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_name        VARCHAR(100) NOT NULL UNIQUE,
    start_date          DATE,
    end_date            DATE,
    hours_per_day       NUMERIC(4,1),
    modules_per_tracker INTEGER,
    mw_per_tracker      NUMERIC(10,5),
    working_days        INTEGER[],
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS dim_site (
    site_id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id           UUID NOT NULL REFERENCES dim_project(project_id) ON DELETE CASCADE,
    site_name            VARCHAR(200) NOT NULL,
    latitude             NUMERIC(10,6),
    longitude            NUMERIC(10,6),
    tracker_product_type VARCHAR(100),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS dim_zone (
    zone_id    UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id UUID NOT NULL REFERENCES dim_project(project_id) ON DELETE CASCADE,
    zone_code  VARCHAR(10) NOT NULL,
    zone_label VARCHAR(50),
    UNIQUE (project_id, zone_code)
);

CREATE TABLE IF NOT EXISTS dim_tracker (
    tracker_id       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id       UUID NOT NULL REFERENCES dim_project(project_id) ON DELETE CASCADE,
    table_name       VARCHAR(50) NOT NULL,
    x_coord          NUMERIC(15,3),
    y_coord          NUMERIC(15,3),
    latitude         NUMERIC(10,6),
    longitude        NUMERIC(10,6),
    module_wattage   INTEGER,
    module_name      VARCHAR(200),
    table_type       VARCHAR(20),
    string_size      INTEGER,
    string_qty       INTEGER,
    top_right_lat    NUMERIC(10,6),
    top_right_lng    NUMERIC(10,6),
    bottom_left_lat  NUMERIC(10,6),
    bottom_left_lng  NUMERIC(10,6),
    sundat_x         NUMERIC(15,3),
    sundat_y         NUMERIC(15,3),
    sundat_z         NUMERIC(15,3),
    UNIQUE (project_id, table_name)
);

CREATE INDEX IF NOT EXISTS idx_tracker_name_trgm
    ON dim_tracker USING gin (table_name gin_trgm_ops);

CREATE TABLE IF NOT EXISTS dim_stage (
    stage_id    SMALLINT PRIMARY KEY,
    stage_key   VARCHAR(20) NOT NULL UNIQUE,
    stage_name  VARCHAR(50) NOT NULL,
    stage_order SMALLINT NOT NULL
);

CREATE TABLE IF NOT EXISTS dim_installation_step (
    step_id        SMALLINT PRIMARY KEY,
    step_key       VARCHAR(40) NOT NULL UNIQUE,
    step_name      VARCHAR(100) NOT NULL,
    parent_stage_id SMALLINT NOT NULL REFERENCES dim_stage(stage_id),
    step_order     SMALLINT NOT NULL
);

-- ─────────────────────────────────────────────
-- FACT TABLES
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS fact_flight (
    flight_id    UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id   UUID NOT NULL REFERENCES dim_project(project_id) ON DELETE CASCADE,
    flight_date  DATE NOT NULL,
    folder_name  VARCHAR(200) NOT NULL,
    flight_label VARCHAR(200),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project_id, folder_name)
);

CREATE INDEX IF NOT EXISTS idx_flight_date
    ON fact_flight (project_id, flight_date);

CREATE TABLE IF NOT EXISTS fact_flight_zone (
    flight_zone_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    flight_id      UUID NOT NULL REFERENCES fact_flight(flight_id) ON DELETE CASCADE,
    zone_id        UUID NOT NULL REFERENCES dim_zone(zone_id) ON DELETE CASCADE,
    UNIQUE (flight_id, zone_id)
);

CREATE TABLE IF NOT EXISTS fact_pipeline_run (
    run_id                       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    flight_id                    UUID NOT NULL REFERENCES fact_flight(flight_id) ON DELETE CASCADE,
    zone_id                      UUID NOT NULL REFERENCES dim_zone(zone_id) ON DELETE CASCADE,
    seed_tracker_name            VARCHAR(80) NOT NULL,
    canvas_width                 INTEGER,
    canvas_height                INTEGER,
    num_source_images            INTEGER,
    total_detections             INTEGER,
    source_images                JSONB,
    timing_image_selection_s     NUMERIC(10,4),
    timing_stitch_and_axes_s     NUMERIC(10,4),
    timing_detection_s           NUMERIC(10,4),
    timing_assignment_and_bbox_s NUMERIC(10,4),
    timing_status_calculation_s  NUMERIC(10,4),
    created_at                   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (flight_id, zone_id, seed_tracker_name)
);

CREATE TABLE IF NOT EXISTS fact_tracker_status (
    tracker_status_id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    flight_id                        UUID NOT NULL REFERENCES fact_flight(flight_id) ON DELETE CASCADE,
    zone_id                          UUID NOT NULL REFERENCES dim_zone(zone_id) ON DELETE CASCADE,
    pipeline_run_id                  UUID REFERENCES fact_pipeline_run(run_id) ON DELETE SET NULL,
    tracker_name                     VARCHAR(80) NOT NULL,
    source_format                    VARCHAR(10) NOT NULL DEFAULT 'csv',
    pile_installation                VARCHAR(20),
    lower_journal_installation       VARCHAR(20),
    slew_drive_installation          VARCHAR(20),
    torque_tube_installation         VARCHAR(20),
    torque_tube_coupler_installation VARCHAR(20),
    upper_journal_installation       VARCHAR(20),
    module_rail_installation         VARCHAR(20),
    pony_panel_installation          VARCHAR(20),
    solar_module_installation        VARCHAR(20),
    current_stage                    VARCHAR(20),
    current_status                   VARCHAR(20),
    UNIQUE (flight_id, zone_id, tracker_name)
);

CREATE INDEX IF NOT EXISTS idx_tracker_status_stage
    ON fact_tracker_status (flight_id, current_stage, current_status);

CREATE INDEX IF NOT EXISTS idx_tracker_status_zone
    ON fact_tracker_status (zone_id, current_stage);

CREATE TABLE IF NOT EXISTS fact_manpower_daily (
    manpower_id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id           UUID NOT NULL REFERENCES dim_project(project_id) ON DELETE CASCADE,
    work_date            DATE NOT NULL,
    pile_workers         INTEGER NOT NULL DEFAULT 0,
    torque_tube_workers  INTEGER NOT NULL DEFAULT 0,
    module_rails_workers INTEGER NOT NULL DEFAULT 0,
    solar_panel_workers  INTEGER NOT NULL DEFAULT 0,
    total_workers        INTEGER NOT NULL DEFAULT 0,
    is_manual_date       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project_id, work_date)
);

CREATE TABLE IF NOT EXISTS fact_stage_milestone (
    milestone_id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id             UUID NOT NULL REFERENCES dim_project(project_id) ON DELETE CASCADE,
    stage_id               SMALLINT NOT NULL REFERENCES dim_stage(stage_id),
    actual_completion_date DATE,
    UNIQUE (project_id, stage_id)
);

CREATE TABLE IF NOT EXISTS fact_processing_timing (
    timing_id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    flight_id                  UUID NOT NULL REFERENCES fact_flight(flight_id) ON DELETE CASCADE,
    zone_id                    UUID NOT NULL REFERENCES dim_zone(zone_id) ON DELETE CASCADE,
    extraction_seconds         NUMERIC(10,2),
    construction_status_seconds NUMERIC(10,2),
    total_seconds              NUMERIC(10,2),
    UNIQUE (flight_id, zone_id)
);

CREATE TABLE IF NOT EXISTS fact_tracker_extraction (
    extraction_id    UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    flight_id        UUID NOT NULL REFERENCES fact_flight(flight_id) ON DELETE CASCADE,
    zone_id          UUID NOT NULL REFERENCES dim_zone(zone_id) ON DELETE CASCADE,
    tracker_name     VARCHAR(80) NOT NULL,
    best_rank_index  INTEGER,
    saved_count      INTEGER,
    rejected_count   INTEGER,
    total_processed  INTEGER,
    extraction_detail JSONB,
    UNIQUE (flight_id, zone_id, tracker_name)
);
