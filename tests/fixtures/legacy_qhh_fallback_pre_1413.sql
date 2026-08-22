-- FROZEN parity baseline -- never update alongside production SQL.
--
-- Provenance: the single-statement authoritative fallback of
-- PsycopgForecastStore._fetch_latest_qhh_display_candidates as it stood at
-- commit 90dc4a7e (packages/common/forecast_store.py), the first parent of the
-- issue #1413 merge 6f12117b. Issues: #1413 (the pushdown rewrite that replaced
-- it), #1414 (this oracle).
--
-- Extraction method (mechanical, no hand editing): the body between the opening
-- and closing triple quotes of the self._fetch_all(...) statement was copied
-- verbatim, and the single line holding the identity_sql f-string placeholder
-- was deleted -- identity=None is the only mode under test and that line was the
-- only brace pair in the body. Nothing else was dedented, reflowed or reworded.
-- What remains binds six positional placeholders, in the legacy call site's
-- order: expected horizon hours, basin_id, source_id, candidate limit, the MVP
-- station variable list, and its length.
--
-- FROZEN rule: this file is the comparison baseline for
-- tests/test_display_coverage_residual_debt_integration.py. It must NEVER be
-- re-synchronised with packages/common/forecast_store.py to make a parity test
-- pass -- an oracle that tracks the code under test asserts nothing. If a
-- production change makes parity fail, the production change is what needs
-- explaining.
--
-- Known divergences from today's production statement (so nobody assumes the
-- deleted identity line is the only delta):
--   (a) #1413 pushdown shape: production now runs a cheap scalar header
--       prefetch and binds its results as literal scan predicates in a second
--       statement; this text is the one-statement predecessor. That difference
--       is precisely what the parity tests exist to measure.
--   (b) #1442 surrogate-key join: production's candidate CTE additionally
--       selects run_key, basin_version_key and river_network_version_key and
--       joins hydro.river_timeseries on them; this text has none of the three
--       and still joins that table on the text identity columns. The parity
--       helper pops exactly those three columns off the production rows and
--       asserts the popped set, so a fourth new column reddens the test rather
--       than being silently tolerated. That same shape entails the
--       hydro_coverage rollup's authority re-resolution INNER JOINs, which
--       read the text ids back out of the surrogate keys; those joins are
--       lossless, because every key reaching the rollup arrived through the
--       key join to candidate_runs, whose keys were themselves read out of
--       those very authority tables, so no row can fail to match.
--   (c) #1340/#1442 enum dual-write column: production's river scan carries an
--       extra conjunct, rt.variable_e = 'q_down'::hydro.river_variable, beside
--       its rt.variable = 'q_down' text twin -- and it is that text twin, not
--       the enum, that production annotates as a transitional "remove with
--       #1342" pushdown aid; variable_e is the enum authority that outlives it.
--       This text has only the text conjunct. Consequence: a row with variable =
--       'q_down' and variable_e IS NULL is visible to this frozen statement
--       and invisible to production. Those are the pre-#1340 text-only rows:
--       000050 added variable_e nullable, and the SET NOT NULL that would
--       forbid them lives only inside
--       hydro.cutover_river_identity_normalization(), which the migration
--       chain never calls. Bounded twice, though. It cannot redden the parity
--       tests, because every river row they seed goes through
--       tests/integration_helpers.py:insert_river_timeseries_dual_written,
--       which always writes variable_e. And live un-backfilled rows have
--       run_key and variable_e NULL together -- one writer, one SET list -- so
--       the (b) key join already excludes them, an exclusion that is itself a
--       recorded contract -- see the out-of-boundary river_timeseries
--       consumers requirement in
--       openspec/specs/river-identity-normalization/spec.md: NULL-key legacy
--       rows being invisible to key filtering is a sanctioned, time-bounded
--       exclusion, not data loss.
--
-- Re-freeze / retire rule (#1342): the text identity columns this statement
-- joins on (rt.run_id, rt.basin_version_id, rt.river_network_version_id) are
-- scheduled for retirement. When they go, this statement stops executing at
-- all. The change that removes them owns the decision to re-freeze this file
-- against the then-current fallback or to retire the oracle with a recorded
-- reason; it must not be "fixed" by editing the SQL below to match production.
            WITH candidate_runs AS (
                SELECT
                    h.run_id,
                    h.run_type,
                    h.scenario_id,
                    h.model_id,
                    h.basin_version_id,
                    h.forcing_version_id,
                    h.source_id,
                    h.cycle_time,
                    h.start_time AS run_start_time,
                    h.end_time AS run_end_time,
                    h.status,
                    h.created_at AS run_created_at,
                    h.updated_at AS run_updated_at,
                    mi.river_network_version_id,
                    mi.basin_version_id AS model_basin_version_id,
                    bv.basin_id,
                    rnv.basin_version_id AS river_network_basin_version_id,
                    COALESCE(
                        CASE WHEN mi.resource_profile->>'output_segment_count' ~ '^[0-9]+$'
                            THEN (mi.resource_profile->>'output_segment_count')::integer END,
                        CASE WHEN mi.resource_profile->>'shud_output_segment_count' ~ '^[0-9]+$'
                            THEN (mi.resource_profile->>'shud_output_segment_count')::integer END,
                        CASE WHEN mi.resource_profile->>'shud_output_river_count' ~ '^[0-9]+$'
                            THEN (mi.resource_profile->>'shud_output_river_count')::integer END,
                        CASE WHEN mi.resource_profile->'output_river'->>'output_segment_count' ~ '^[0-9]+$'
                            THEN (mi.resource_profile->'output_river'->>'output_segment_count')::integer END,
                        CASE WHEN mi.resource_profile->'output_river'->>'segment_count' ~ '^[0-9]+$'
                            THEN (mi.resource_profile->'output_river'->>'segment_count')::integer END,
                        rnv.segment_count
                    ) AS expected_segment_count,
                    fv.forcing_version_id AS fv_forcing_version_id,
                    fv.model_id AS forcing_model_id,
                    fv.source_id AS forcing_source_id,
                    fv.cycle_time AS forcing_cycle_time,
                    fv.start_time AS forcing_start_time,
                    fv.end_time AS forcing_end_time,
                    fv.station_count AS expected_station_count,
                    fv.checksum AS forcing_checksum,
                    GREATEST(h.cycle_time, h.start_time, fv.start_time) AS display_start_time,
                    LEAST(
                        h.end_time,
                        fv.end_time,
                        h.cycle_time + (%s * INTERVAL '1 hour')
                    ) AS display_end_time
                FROM hydro.hydro_run h
                JOIN core.basin_version bv
                  ON bv.basin_version_id = h.basin_version_id
                LEFT JOIN core.model_instance mi
                  ON mi.model_id = h.model_id
                LEFT JOIN core.river_network_version rnv
                  ON rnv.river_network_version_id = mi.river_network_version_id
                LEFT JOIN met.forcing_version fv
                  ON fv.forcing_version_id = h.forcing_version_id
                WHERE bv.basin_id = %s
                  AND h.run_type = 'forecast'
                  AND h.status IN ('succeeded', 'parsed', 'published')
                  AND LOWER(h.source_id) = LOWER(%s)
                  AND h.cycle_time IS NOT NULL
                ORDER BY h.cycle_time DESC, h.run_id DESC
                LIMIT %s
            ),
            station_sample_rows AS (
                SELECT
                    cr.run_id,
                    cr.model_id,
                    cr.display_start_time,
                    cr.display_end_time,
                    fst.forcing_version_id,
                    fst.basin_version_id,
                    LOWER(fst.source_id) AS station_source_id,
                    fst.station_id,
                    fst.variable,
                    cr.expected_station_count,
                    fst.valid_time,
                    fst.unit,
                    fst.quality_flag
                FROM met.forcing_station_timeseries fst
                JOIN candidate_runs cr
                  ON cr.forcing_version_id = fst.forcing_version_id
                 AND fst.basin_version_id = cr.basin_version_id
                 AND LOWER(fst.source_id) = LOWER(cr.source_id)
                WHERE fst.variable = ANY(%s)
                  AND fst.valid_time >= cr.display_start_time
                  AND fst.valid_time <= cr.display_end_time
                  AND EXISTS (
                      SELECT 1
                      FROM met.interp_weight iw
                      WHERE iw.model_id = cr.model_id
                        AND iw.station_id = fst.station_id
                        AND iw.variable = fst.variable
                        AND LOWER(iw.source_id) = LOWER(cr.source_id)
                  )
            ),
            station_identity_coverage AS (
                SELECT
                    run_id,
                    model_id,
                    display_start_time,
                    display_end_time,
                    forcing_version_id,
                    basin_version_id,
                    station_source_id,
                    variable,
                    station_id,
                    COUNT(*) AS sample_count,
                    MIN(valid_time) AS valid_time_start,
                    MAX(valid_time) AS valid_time_end
                FROM station_sample_rows
                GROUP BY
                    run_id,
                    model_id,
                    display_start_time,
                    display_end_time,
                    forcing_version_id,
                    basin_version_id,
                    station_source_id,
                    variable,
                    station_id
            ),
            station_time_coverage AS (
                SELECT
                    run_id,
                    model_id,
                    display_start_time,
                    display_end_time,
                    forcing_version_id,
                    basin_version_id,
                    station_source_id,
                    variable,
                    expected_station_count,
                    valid_time,
                    COUNT(DISTINCT station_id) AS station_count
                FROM station_sample_rows
                GROUP BY
                    run_id,
                    model_id,
                    display_start_time,
                    display_end_time,
                    forcing_version_id,
                    basin_version_id,
                    station_source_id,
                    variable,
                    expected_station_count,
                    valid_time
            ),
            station_variable_complete_times AS (
                SELECT
                    run_id,
                    model_id,
                    display_start_time,
                    display_end_time,
                    forcing_version_id,
                    basin_version_id,
                    station_source_id,
                    variable,
                    valid_time
                FROM station_time_coverage
                WHERE expected_station_count IS NOT NULL
                  AND station_count = expected_station_count
            ),
            station_variable_common_times AS (
                SELECT
                    run_id,
                    model_id,
                    display_start_time,
                    display_end_time,
                    forcing_version_id,
                    basin_version_id,
                    station_source_id,
                    variable,
                    MIN(valid_time) AS valid_time_start,
                    MAX(valid_time) AS valid_time_end
                FROM station_variable_complete_times
                GROUP BY
                    run_id,
                    model_id,
                    display_start_time,
                    display_end_time,
                    forcing_version_id,
                    basin_version_id,
                    station_source_id,
                    variable
            ),
            station_all_variable_complete_times AS (
                SELECT
                    run_id,
                    model_id,
                    display_start_time,
                    display_end_time,
                    forcing_version_id,
                    basin_version_id,
                    station_source_id,
                    valid_time,
                    COUNT(DISTINCT variable) AS complete_variable_count
                FROM station_variable_complete_times
                GROUP BY
                    run_id,
                    model_id,
                    display_start_time,
                    display_end_time,
                    forcing_version_id,
                    basin_version_id,
                    station_source_id,
                    valid_time
                HAVING COUNT(DISTINCT variable) = %s
            ),
            station_identity_rollup AS (
                SELECT
                    run_id,
                    model_id,
                    display_start_time,
                    display_end_time,
                    forcing_version_id,
                    basin_version_id,
                    station_source_id,
                    COUNT(DISTINCT station_id) AS station_count,
                    SUM(sample_count) AS station_sample_count
                FROM station_identity_coverage
                GROUP BY
                    run_id,
                    model_id,
                    display_start_time,
                    display_end_time,
                    forcing_version_id,
                    basin_version_id,
                    station_source_id
            ),
            station_common_window AS (
                SELECT
                    run_id,
                    model_id,
                    display_start_time,
                    display_end_time,
                    forcing_version_id,
                    basin_version_id,
                    station_source_id,
                    MIN(valid_time) AS station_valid_time_start,
                    MAX(valid_time) AS station_valid_time_end
                FROM station_all_variable_complete_times
                GROUP BY
                    run_id,
                    model_id,
                    display_start_time,
                    display_end_time,
                    forcing_version_id,
                    basin_version_id,
                    station_source_id
            ),
            station_coverage AS (
                SELECT
                    rollup.run_id,
                    rollup.model_id,
                    rollup.display_start_time,
                    rollup.display_end_time,
                    rollup.forcing_version_id,
                    rollup.basin_version_id,
                    rollup.station_source_id,
                    rollup.station_count,
                    rollup.station_sample_count,
                    common_window.station_valid_time_start,
                    common_window.station_valid_time_end
                FROM station_identity_rollup rollup
                LEFT JOIN station_common_window common_window
                  ON common_window.run_id = rollup.run_id
                 AND common_window.model_id = rollup.model_id
                 AND common_window.display_start_time = rollup.display_start_time
                 AND common_window.display_end_time = rollup.display_end_time
                 AND common_window.forcing_version_id = rollup.forcing_version_id
                 AND common_window.basin_version_id = rollup.basin_version_id
                 AND common_window.station_source_id = rollup.station_source_id
            ),
            station_variable_sample_stats AS (
                SELECT
                    run_id,
                    model_id,
                    display_start_time,
                    display_end_time,
                    forcing_version_id,
                    basin_version_id,
                    station_source_id,
                    variable,
                    COUNT(*) AS sample_count,
                    COUNT(DISTINCT NULLIF(BTRIM(unit), '')) AS unit_count,
                    COUNT(DISTINCT NULLIF(BTRIM(quality_flag), '')) AS quality_flag_count,
                    SUM(CASE WHEN unit IS NULL OR BTRIM(unit) = '' THEN 1 ELSE 0 END)
                        AS missing_unit_samples,
                    SUM(CASE WHEN quality_flag IS NULL OR BTRIM(quality_flag) = '' THEN 1 ELSE 0 END)
                        AS missing_quality_flag_samples
                FROM station_sample_rows
                GROUP BY
                    run_id,
                    model_id,
                    display_start_time,
                    display_end_time,
                    forcing_version_id,
                    basin_version_id,
                    station_source_id,
                    variable
            ),
            station_variable_identity_stats AS (
                SELECT
                    run_id,
                    model_id,
                    display_start_time,
                    display_end_time,
                    forcing_version_id,
                    basin_version_id,
                    station_source_id,
                    variable,
                    COUNT(DISTINCT station_id) AS station_count
                FROM station_identity_coverage
                GROUP BY
                    run_id,
                    model_id,
                    display_start_time,
                    display_end_time,
                    forcing_version_id,
                    basin_version_id,
                    station_source_id,
                    variable
            ),
            station_variable_coverage AS (
                SELECT
                    identity_stats.run_id,
                    identity_stats.model_id,
                    identity_stats.display_start_time,
                    identity_stats.display_end_time,
                    identity_stats.forcing_version_id,
                    identity_stats.basin_version_id,
                    identity_stats.station_source_id,
                    jsonb_agg(
                        jsonb_build_object(
                            'variable', identity_stats.variable,
                            'station_count', identity_stats.station_count,
                            'sample_count', sample_stats.sample_count,
                            'unit_count', sample_stats.unit_count,
                            'quality_flag_count', sample_stats.quality_flag_count,
                            'missing_unit_samples', sample_stats.missing_unit_samples,
                            'missing_quality_flag_samples', sample_stats.missing_quality_flag_samples,
                            'valid_time_start', common_times.valid_time_start,
                            'valid_time_end', common_times.valid_time_end
                        )
                        ORDER BY identity_stats.variable
                    ) AS station_variable_coverage
                FROM station_variable_identity_stats identity_stats
                JOIN station_variable_sample_stats sample_stats
                  ON sample_stats.run_id = identity_stats.run_id
                 AND sample_stats.model_id = identity_stats.model_id
                 AND sample_stats.display_start_time = identity_stats.display_start_time
                 AND sample_stats.display_end_time = identity_stats.display_end_time
                 AND sample_stats.forcing_version_id = identity_stats.forcing_version_id
                 AND sample_stats.basin_version_id = identity_stats.basin_version_id
                 AND sample_stats.station_source_id = identity_stats.station_source_id
                 AND sample_stats.variable = identity_stats.variable
                LEFT JOIN station_variable_common_times common_times
                  ON common_times.run_id = identity_stats.run_id
                 AND common_times.model_id = identity_stats.model_id
                 AND common_times.display_start_time = identity_stats.display_start_time
                 AND common_times.display_end_time = identity_stats.display_end_time
                 AND common_times.forcing_version_id = identity_stats.forcing_version_id
                 AND common_times.basin_version_id = identity_stats.basin_version_id
                 AND common_times.station_source_id = identity_stats.station_source_id
                 AND common_times.variable = identity_stats.variable
                GROUP BY
                    identity_stats.run_id,
                    identity_stats.model_id,
                    identity_stats.display_start_time,
                    identity_stats.display_end_time,
                    identity_stats.forcing_version_id,
                    identity_stats.basin_version_id,
                    identity_stats.station_source_id
            ),
            river_sample_rows AS (
                SELECT
                    rt.run_id,
                    rt.basin_version_id,
                    rt.river_network_version_id,
                    rt.river_segment_id,
                    cr.expected_segment_count,
                    rt.valid_time,
                    rt.lead_time_hours
                FROM hydro.river_timeseries rt
                JOIN candidate_runs cr
                  ON cr.run_id = rt.run_id
                 AND cr.basin_version_id = rt.basin_version_id
                 AND cr.river_network_version_id = rt.river_network_version_id
                WHERE rt.variable = 'q_down'
                  AND rt.valid_time >= cr.display_start_time
                  AND rt.valid_time <= cr.display_end_time
            ),
            river_identity_coverage AS (
                SELECT
                    run_id,
                    basin_version_id,
                    river_network_version_id,
                    river_segment_id,
                    COUNT(*) AS sample_count,
                    MIN(valid_time) AS valid_time_start,
                    MAX(valid_time) AS valid_time_end,
                    MIN(lead_time_hours) AS min_lead_time_hours,
                    MAX(lead_time_hours) AS max_lead_time_hours
                FROM river_sample_rows
                GROUP BY run_id, basin_version_id, river_network_version_id, river_segment_id
            ),
            river_time_coverage AS (
                SELECT
                    run_id,
                    basin_version_id,
                    river_network_version_id,
                    expected_segment_count,
                    valid_time,
                    COUNT(DISTINCT river_segment_id) AS segment_count
                FROM river_sample_rows
                GROUP BY run_id, basin_version_id, river_network_version_id, expected_segment_count, valid_time
            ),
            river_common_window AS (
                SELECT
                    run_id,
                    basin_version_id,
                    river_network_version_id,
                    MIN(valid_time) AS river_valid_time_start,
                    MAX(valid_time) AS river_valid_time_end
                FROM river_time_coverage
                WHERE expected_segment_count IS NOT NULL
                  AND segment_count = expected_segment_count
                GROUP BY run_id, basin_version_id, river_network_version_id
            ),
            river_identity_rollup AS (
                SELECT
                    run_id,
                    basin_version_id,
                    river_network_version_id,
                    COUNT(DISTINCT river_segment_id) AS segment_count,
                    SUM(sample_count) AS river_sample_count,
                    MAX(min_lead_time_hours) AS min_lead_time_hours,
                    MIN(max_lead_time_hours) AS max_lead_time_hours
                FROM river_identity_coverage
                GROUP BY run_id, basin_version_id, river_network_version_id
            ),
            hydro_coverage AS (
                SELECT
                    rollup.run_id,
                    rollup.basin_version_id,
                    rollup.river_network_version_id,
                    rollup.segment_count,
                    rollup.river_sample_count,
                    common_window.river_valid_time_start,
                    common_window.river_valid_time_end,
                    rollup.min_lead_time_hours,
                    rollup.max_lead_time_hours
                FROM river_identity_rollup rollup
                LEFT JOIN river_common_window common_window
                  ON common_window.run_id = rollup.run_id
                 AND common_window.basin_version_id = rollup.basin_version_id
                 AND common_window.river_network_version_id = rollup.river_network_version_id
            )
            SELECT
                cr.*,
                COALESCE(sc.station_count, 0) AS station_count,
                COALESCE(sc.station_sample_count, 0) AS station_sample_count,
                sc.run_id AS station_run_id,
                sc.model_id AS station_model_id,
                sc.display_start_time AS station_display_start_time,
                sc.display_end_time AS station_display_end_time,
                sc.basin_version_id AS station_basin_version_id,
                sc.station_source_id,
                sc.station_valid_time_start,
                sc.station_valid_time_end,
                COALESCE(svc.station_variable_coverage, '[]'::jsonb) AS station_variable_coverage,
                COALESCE(hc.segment_count, 0) AS segment_count,
                COALESCE(hc.river_sample_count, 0) AS river_sample_count,
                hc.river_valid_time_start,
                hc.river_valid_time_end,
                hc.min_lead_time_hours,
                hc.max_lead_time_hours
            FROM candidate_runs cr
            LEFT JOIN station_coverage sc
              ON sc.run_id = cr.run_id
             AND sc.model_id = cr.model_id
             AND sc.display_start_time = cr.display_start_time
             AND sc.display_end_time = cr.display_end_time
             AND sc.forcing_version_id = cr.forcing_version_id
             AND sc.basin_version_id = cr.basin_version_id
             AND sc.station_source_id = LOWER(cr.source_id)
            LEFT JOIN station_variable_coverage svc
              ON svc.run_id = cr.run_id
             AND svc.model_id = cr.model_id
             AND svc.display_start_time = cr.display_start_time
             AND svc.display_end_time = cr.display_end_time
             AND svc.forcing_version_id = cr.forcing_version_id
             AND svc.basin_version_id = cr.basin_version_id
             AND svc.station_source_id = LOWER(cr.source_id)
            LEFT JOIN hydro_coverage hc
              ON hc.run_id = cr.run_id
             AND hc.basin_version_id = cr.basin_version_id
             AND hc.river_network_version_id = cr.river_network_version_id
            ORDER BY cr.cycle_time DESC, cr.run_id DESC
