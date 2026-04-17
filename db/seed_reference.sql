-- =============================================================
-- Seed static reference / dimension data
-- Safe to re-run (uses ON CONFLICT DO NOTHING)
-- =============================================================

-- 4 construction stages in pipeline order
INSERT INTO dim_stage (stage_id, stage_key, stage_name, stage_order) VALUES
    (1, 'pile',         'Pile Installation',  1),
    (2, 'torque_tube',  'Torque Tube',        2),
    (3, 'module_rails', 'Module Rails',       3),
    (4, 'solar_panel',  'Solar Panel',        4)
ON CONFLICT (stage_id) DO NOTHING;

-- 9 installation steps mapped to their parent stage
INSERT INTO dim_installation_step (step_id, step_key, step_name, parent_stage_id, step_order) VALUES
    (1,  'pile_installation',                'Pile Installation',                1, 1),
    (2,  'lower_journal_installation',       'Lower Journal Installation',       2, 2),
    (3,  'slew_drive_installation',          'Slew Drive Installation',          2, 3),
    (4,  'torque_tube_installation',         'Torque Tube Installation',         2, 4),
    (5,  'torque_tube_coupler_installation', 'Torque Tube Coupler Installation', 2, 5),
    (6,  'upper_journal_installation',       'Upper Journal Installation',       2, 6),
    (7,  'module_rail_installation',         'Module Rail Installation',         3, 7),
    (8,  'pony_panel_installation',          'Pony Panel Installation',          3, 8),
    (9,  'solar_module_installation',        'Solar Module Installation',        4, 9)
ON CONFLICT (step_id) DO NOTHING;
