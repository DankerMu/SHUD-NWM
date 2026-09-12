
        WITH candidate_runs AS (
            SELECT
                h.run_id,
                h.model_id,
                h.basin_version_id,
                h.forcing_version_id,
                h.source_id,
                h.cycle_time,
                mi.river_network_version_id,
                -- Surrogate keys for the river fact-table join (issue #1341).
                -- candidate_runs already reads all three authority tables, so
                -- the keys cost no extra join; the river scan below joins on
                -- them instead of on the repeated text identity columns.
                h.run_key,
                bv.basin_version_key,
                rnv.river_network_version_key,
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
                fv.station_count AS expected_station_count,
                GREATEST(h.cycle_time, h.start_time, fv.start_time) AS display_start_time,
                LEAST(
                    h.end_time,
                    fv.end_time,
                    h.cycle_time + (%(horizon)s * INTERVAL '1 hour')
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
            WHERE (%(basin_id)s IS NULL OR bv.basin_id = %(basin_id)s)
              AND h.run_type = 'forecast'
              AND h.status IN ('succeeded', 'parsed', 'published')
              AND h.cycle_time IS NOT NULL
              AND (%(run_id)s IS NULL OR h.run_id = %(run_id)s)
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
            WHERE fst.variable = ANY(%(variables)s)
              AND fst.valid_time >= cr.display_start_time
              AND fst.valid_time <= cr.display_end_time
              AND (%(scan_forcing_version_id)s IS NULL
                   OR fst.forcing_version_id = %(scan_forcing_version_id)s)
              AND (%(scan_basin_version_id)s IS NULL
                   OR fst.basin_version_id = %(scan_basin_version_id)s)
              AND (%(scan_source_id_lower)s IS NULL
                   OR LOWER(fst.source_id) = %(scan_source_id_lower)s)
              AND (%(scan_display_start)s IS NULL
                   OR fst.valid_time >= %(scan_display_start)s)
              AND (%(scan_display_end)s IS NULL
                   OR fst.valid_time <= %(scan_display_end)s)
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
                run_id, model_id, display_start_time, display_end_time,
                forcing_version_id, basin_version_id, station_source_id,
                variable, station_id,
                COUNT(*) AS sample_count,
                MIN(valid_time) AS valid_time_start,
                MAX(valid_time) AS valid_time_end
            FROM station_sample_rows
            GROUP BY
                run_id, model_id, display_start_time, display_end_time,
                forcing_version_id, basin_version_id, station_source_id,
                variable, station_id
        ),
        station_time_coverage AS (
            SELECT
                run_id, model_id, display_start_time, display_end_time,
                forcing_version_id, basin_version_id, station_source_id,
                variable, expected_station_count, valid_time,
                COUNT(DISTINCT station_id) AS station_count
            FROM station_sample_rows
            GROUP BY
                run_id, model_id, display_start_time, display_end_time,
                forcing_version_id, basin_version_id, station_source_id,
                variable, expected_station_count, valid_time
        ),
        station_variable_complete_times AS (
            SELECT
                run_id, model_id, display_start_time, display_end_time,
                forcing_version_id, basin_version_id, station_source_id,
                variable, valid_time
            FROM station_time_coverage
            WHERE expected_station_count IS NOT NULL
              AND station_count = expected_station_count
        ),
        station_variable_common_times AS (
            SELECT
                run_id, model_id, display_start_time, display_end_time,
                forcing_version_id, basin_version_id, station_source_id,
                variable,
                MIN(valid_time) AS valid_time_start,
                MAX(valid_time) AS valid_time_end
            FROM station_variable_complete_times
            GROUP BY
                run_id, model_id, display_start_time, display_end_time,
                forcing_version_id, basin_version_id, station_source_id,
                variable
        ),
        station_all_variable_complete_times AS (
            SELECT
                run_id, model_id, display_start_time, display_end_time,
                forcing_version_id, basin_version_id, station_source_id,
                valid_time,
                COUNT(DISTINCT variable) AS complete_variable_count
            FROM station_variable_complete_times
            GROUP BY
                run_id, model_id, display_start_time, display_end_time,
                forcing_version_id, basin_version_id, station_source_id,
                valid_time
            HAVING COUNT(DISTINCT variable) = %(variable_count)s
        ),
        station_identity_rollup AS (
            SELECT
                run_id, model_id, display_start_time, display_end_time,
                forcing_version_id, basin_version_id, station_source_id,
                COUNT(DISTINCT station_id) AS station_count,
                SUM(sample_count) AS station_sample_count
            FROM station_identity_coverage
            GROUP BY
                run_id, model_id, display_start_time, display_end_time,
                forcing_version_id, basin_version_id, station_source_id
        ),
        station_common_window AS (
            SELECT
                run_id, model_id, display_start_time, display_end_time,
                forcing_version_id, basin_version_id, station_source_id,
                MIN(valid_time) AS station_valid_time_start,
                MAX(valid_time) AS station_valid_time_end
            FROM station_all_variable_complete_times
            GROUP BY
                run_id, model_id, display_start_time, display_end_time,
                forcing_version_id, basin_version_id, station_source_id
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
                run_id, model_id, display_start_time, display_end_time,
                forcing_version_id, basin_version_id, station_source_id,
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
                run_id, model_id, display_start_time, display_end_time,
                forcing_version_id, basin_version_id, station_source_id,
                variable
        ),
        station_variable_identity_stats AS (
            SELECT
                run_id, model_id, display_start_time, display_end_time,
                forcing_version_id, basin_version_id, station_source_id,
                variable,
                COUNT(DISTINCT station_id) AS station_count
            FROM station_identity_coverage
            GROUP BY
                run_id, model_id, display_start_time, display_end_time,
                forcing_version_id, basin_version_id, station_source_id,
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
        -- River scan on the surrogate keys (issue #1341, index 000051). The
        -- scan_* pushdown parameters keep their text semantics — they are
        -- still the run's scalar text identity, resolved to keys inside the
        -- query by a scalar subquery the planner runs once. A NULL binding
        -- folds the guard away exactly as before; a text value that resolves
        -- to nothing yields the empty scan the text predicate yielded.
        -- The whole river chain groups by keys and the text identity is
        -- reconstructed once, at the rollup, by joining the authority tables
        -- (join-and-reconstruct), so `coverage` below is untouched.
        -- Do not spell a psycopg2 placeholder inside a comment in this string:
        -- psycopg2 interpolates the entire statement, comments included, and
        -- an unbound name there raises at execute time.
        -- Which conjuncts are transitional pushdown aids is stated per line by
        -- the removal marker below, one marker per aid (#1980); this paragraph
        -- states only WHY they exist. Compression still segments compressed
        -- chunks by the text columns, so a pure-key predicate cannot be pushed
        -- into them. Each aid sits in the same conjunction as its key
        -- counterpart, so it only narrows, and all of them go with the text
        -- columns in #1342. They are CONSTANT-valued predicates on purpose; the
        -- join to candidate_runs is key-only, because a text join equality is
        -- not pushdown material and a text fact join is forbidden.
        river_sample_rows AS (
            SELECT
                rt.run_key,
                rt.basin_version_key,
                rt.river_network_version_key,
                rt.river_segment_key,
                cr.expected_segment_count,
                rt.valid_time,
                rt.lead_time_hours
            FROM hydro.river_timeseries rt
            JOIN candidate_runs cr
              ON cr.run_key = rt.run_key
             AND cr.basin_version_key = rt.basin_version_key
             AND cr.river_network_version_key = rt.river_network_version_key
            WHERE rt.variable_e = 'q_down'::hydro.river_variable
              -- transitional compressed-chunk pushdown aid, remove with #1342
              AND rt.variable = 'q_down'
              AND rt.valid_time >= cr.display_start_time
              AND rt.valid_time <= cr.display_end_time
              AND (%(scan_run_id)s IS NULL
                   OR (
                       -- transitional compressed-chunk pushdown aid, remove with #1342
                       rt.run_id = %(scan_run_id)s AND
                       rt.run_key = (SELECT run_key FROM hydro.hydro_run
                                     WHERE run_id = %(scan_run_id)s)))
              AND (%(scan_basin_version_id)s IS NULL
                   OR rt.basin_version_key = (SELECT basin_version_key FROM core.basin_version
                                              WHERE basin_version_id = %(scan_basin_version_id)s))
              AND (%(scan_river_network_version_id)s IS NULL
                   OR (
                       -- transitional compressed-chunk pushdown aid, remove with #1342
                       rt.river_network_version_id = %(scan_river_network_version_id)s AND
                       rt.river_network_version_key = (SELECT river_network_version_key
                                                       FROM core.river_network_version
                                                       WHERE river_network_version_id
                                                             = %(scan_river_network_version_id)s)))
              AND (%(scan_display_start)s IS NULL
                   OR rt.valid_time >= %(scan_display_start)s)
              AND (%(scan_display_end)s IS NULL
                   OR rt.valid_time <= %(scan_display_end)s)
        ),
        river_identity_coverage AS (
            SELECT
                run_key, basin_version_key, river_network_version_key, river_segment_key,
                COUNT(*) AS sample_count,
                MIN(valid_time) AS valid_time_start,
                MAX(valid_time) AS valid_time_end,
                MIN(lead_time_hours) AS min_lead_time_hours,
                MAX(lead_time_hours) AS max_lead_time_hours
            FROM river_sample_rows
            GROUP BY run_key, basin_version_key, river_network_version_key, river_segment_key
        ),
        river_time_coverage AS (
            SELECT
                run_key, basin_version_key, river_network_version_key,
                expected_segment_count, valid_time,
                COUNT(DISTINCT river_segment_key) AS segment_count
            FROM river_sample_rows
            GROUP BY run_key, basin_version_key, river_network_version_key,
                     expected_segment_count, valid_time
        ),
        river_common_window AS (
            SELECT
                run_key, basin_version_key, river_network_version_key,
                MIN(valid_time) AS river_valid_time_start,
                MAX(valid_time) AS river_valid_time_end
            FROM river_time_coverage
            WHERE expected_segment_count IS NOT NULL
              AND segment_count = expected_segment_count
            GROUP BY run_key, basin_version_key, river_network_version_key
        ),
        river_identity_rollup AS (
            SELECT
                run_key, basin_version_key, river_network_version_key,
                COUNT(DISTINCT river_segment_key) AS segment_count,
                SUM(sample_count) AS river_sample_count,
                MAX(min_lead_time_hours) AS min_lead_time_hours,
                MIN(max_lead_time_hours) AS max_lead_time_hours
            FROM river_identity_coverage
            GROUP BY run_key, basin_version_key, river_network_version_key
        ),
        hydro_coverage AS (
            SELECT
                rollup_run.run_id,
                rollup_basin.basin_version_id,
                rollup_network.river_network_version_id,
                rollup.segment_count,
                rollup.river_sample_count,
                common_window.river_valid_time_start,
                common_window.river_valid_time_end,
                rollup.min_lead_time_hours,
                rollup.max_lead_time_hours
            FROM river_identity_rollup rollup
            LEFT JOIN river_common_window common_window
              ON common_window.run_key = rollup.run_key
             AND common_window.basin_version_key = rollup.basin_version_key
             AND common_window.river_network_version_key = rollup.river_network_version_key
            JOIN hydro.hydro_run rollup_run
              ON rollup_run.run_key = rollup.run_key
            JOIN core.basin_version rollup_basin
              ON rollup_basin.basin_version_key = rollup.basin_version_key
            JOIN core.river_network_version rollup_network
              ON rollup_network.river_network_version_key = rollup.river_network_version_key
        ),
        coverage AS (
            SELECT
                cr.run_id,
                COALESCE(sc.station_count, 0) AS station_count,
                COALESCE(sc.station_sample_count, 0) AS station_sample_count,
                sc.station_source_id,
                sc.display_start_time AS station_display_start_time,
                sc.display_end_time AS station_display_end_time,
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
        )

        INSERT INTO hydro.run_display_coverage (
            run_id, station_count, station_sample_count, station_source_id,
            station_display_start_time, station_display_end_time,
            station_valid_time_start, station_valid_time_end,
            station_variable_coverage, segment_count, river_sample_count,
            river_valid_time_start, river_valid_time_end,
            min_lead_time_hours, max_lead_time_hours, refreshed_at
        )
        SELECT
            run_id, station_count, station_sample_count, station_source_id,
            station_display_start_time, station_display_end_time,
            station_valid_time_start, station_valid_time_end,
            station_variable_coverage, segment_count, river_sample_count,
            river_valid_time_start, river_valid_time_end,
            min_lead_time_hours, max_lead_time_hours, now()
        FROM coverage
        ON CONFLICT (run_id) DO UPDATE SET
            station_count = EXCLUDED.station_count,
            station_sample_count = EXCLUDED.station_sample_count,
            station_source_id = EXCLUDED.station_source_id,
            station_display_start_time = EXCLUDED.station_display_start_time,
            station_display_end_time = EXCLUDED.station_display_end_time,
            station_valid_time_start = EXCLUDED.station_valid_time_start,
            station_valid_time_end = EXCLUDED.station_valid_time_end,
            station_variable_coverage = EXCLUDED.station_variable_coverage,
            segment_count = EXCLUDED.segment_count,
            river_sample_count = EXCLUDED.river_sample_count,
            river_valid_time_start = EXCLUDED.river_valid_time_start,
            river_valid_time_end = EXCLUDED.river_valid_time_end,
            min_lead_time_hours = EXCLUDED.min_lead_time_hours,
            max_lead_time_hours = EXCLUDED.max_lead_time_hours,
            refreshed_at = EXCLUDED.refreshed_at
        WHERE %(force)s
           OR EXCLUDED.segment_count > 0
           OR hydro.run_display_coverage.segment_count = 0
        RETURNING run_id
    