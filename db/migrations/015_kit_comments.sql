-- target: main
-- Kit data dictionary (SPOTDATA/.../03_DATA_DICTIONARY/data_dictionary.csv) as column comments. Metadata only.
COMMENT ON TABLE locations IS 'Synthetic NextEra SPOT challenge data (kit 02_DATA/locations.csv)';
COMMENT ON TABLE assets    IS 'Synthetic NextEra SPOT challenge data (kit 02_DATA/assets.csv); location_id is the asset''s nominal home, actions/readings/anomalies.location_id is where it was inspected';
COMMENT ON TABLE missions  IS 'Synthetic NextEra SPOT challenge data (kit 02_DATA/missions.csv)';
COMMENT ON TABLE actions   IS 'Synthetic NextEra SPOT challenge data (kit 02_DATA/actions.csv)';
COMMENT ON TABLE readings  IS 'Synthetic NextEra SPOT challenge data (kit 02_DATA/readings.csv)';
COMMENT ON TABLE anomalies IS 'Synthetic NextEra SPOT challenge data (kit 02_DATA/anomalies.csv)';
DO $cmt$
DECLARE c record;
BEGIN
  FOR c IN SELECT * FROM (VALUES
    ('missions', 'mission_id', 'Unique synthetic mission identifier.'),
    ('missions', 'mission_name', 'Human-readable synthetic mission name.'),
    ('missions', 'scenario', 'Synthetic comparison scenario: BASELINE or IMPROVED.'),
    ('missions', 'configuration_version', 'Synthetic configuration identifier: V1 or V2.'),
    ('missions', 'start_time', 'Mission start timestamp.'),
    ('missions', 'end_time', 'Mission end timestamp.'),
    ('missions', 'duration_min', 'Mission duration in minutes.'),
    ('missions', 'start_battery_pct', 'Battery percentage at mission start.'),
    ('missions', 'end_battery_pct', 'Battery percentage at mission end.'),
    ('missions', 'mission_status', 'Overall mission status.'),
    ('missions', 'operator_mode', 'Operating mode associated with the mission.'),
    ('actions', 'action_id', 'Unique synthetic action identifier.'),
    ('actions', 'mission_id', 'Mission containing the action.'),
    ('actions', 'sequence', 'Action order within the mission.'),
    ('actions', 'timestamp', 'Synthetic timestamp for the action.'),
    ('actions', 'location_id', 'Location associated with the action.'),
    ('actions', 'asset_id', 'Asset associated with the action.'),
    ('actions', 'action_type', 'Type of action or inspection.'),
    ('actions', 'battery_pct', 'Estimated battery state when action occurred.'),
    ('actions', 'status', 'Action outcome: SUCCESS or FAILED.'),
    ('actions', 'failure_type', 'Failure category when action status is FAILED.'),
    ('actions', 'duration_sec', 'Synthetic action duration in seconds.'),
    ('actions', 'scenario', 'Synthetic comparison scenario.'),
    ('actions', 'configuration_version', 'Synthetic configuration identifier.'),
    ('readings', 'reading_id', 'Unique synthetic reading identifier.'),
    ('readings', 'action_id', 'Action that generated the reading.'),
    ('readings', 'mission_id', 'Mission associated with the reading.'),
    ('readings', 'asset_id', 'Asset associated with the reading.'),
    ('readings', 'location_id', 'Location associated with the reading.'),
    ('readings', 'timestamp', 'Synthetic reading timestamp.'),
    ('readings', 'measurement_type', 'Type of inspection or telemetry measurement.'),
    ('readings', 'value', 'Synthetic measurement value.'),
    ('readings', 'unit', 'Unit associated with the measurement.'),
    ('readings', 'quality', 'Simplified quality indicator.'),
    ('anomalies', 'anomaly_id', 'Unique synthetic anomaly identifier.'),
    ('anomalies', 'action_id', 'Action associated with the anomaly.'),
    ('anomalies', 'mission_id', 'Mission associated with the anomaly.'),
    ('anomalies', 'location_id', 'Location where anomaly was associated.'),
    ('anomalies', 'asset_id', 'Asset associated with the anomaly.'),
    ('anomalies', 'timestamp', 'Synthetic anomaly timestamp.'),
    ('anomalies', 'anomaly_type', 'Synthetic anomaly classification.'),
    ('anomalies', 'severity', 'LOW, MEDIUM, HIGH, or CRITICAL.'),
    ('anomalies', 'operator_review_required', 'Indicates whether review is recommended.'),
    ('anomalies', 'description', 'Synthetic description of the anomaly.'),
    ('locations', 'location_id', 'Unique synthetic waypoint identifier.'),
    ('locations', 'location_name', 'Fictional operating-area name.'),
    ('locations', 'zone_type', 'General fictional area classification.'),
    ('locations', 'floor', 'Synthetic floor number.'),
    ('locations', 'x_coord', 'Synthetic X coordinate for visualization.'),
    ('locations', 'y_coord', 'Synthetic Y coordinate for visualization.'),
    ('locations', 'environment', 'Simplified synthetic operating environment.'),
    ('assets', 'asset_id', 'Unique synthetic equipment identifier.'),
    ('assets', 'asset_name', 'Human-readable fictional equipment name.'),
    ('assets', 'asset_type', 'Synthetic equipment category.'),
    ('assets', 'location_id', 'Location containing the asset.'),
    ('assets', 'criticality', 'Simplified fictional asset criticality.')
  ) AS t(tbl, col, txt) LOOP
    BEGIN
      EXECUTE format('COMMENT ON COLUMN public.%I.%I IS %L', c.tbl, c.col, c.txt || ' (kit data dictionary)');
    EXCEPTION WHEN undefined_column OR undefined_table OR insufficient_privilege THEN
      RAISE WARNING 'comment on %.% skipped: %', c.tbl, c.col, SQLERRM;
    END;
  END LOOP;
END
$cmt$;
