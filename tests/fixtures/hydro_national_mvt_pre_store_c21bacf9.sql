
        WITH bounds AS (
            SELECT ST_TileEnvelope(:z, :x, :y) AS geom_3857
        ),
        source_rows AS NOT MATERIALIZED (
            
            WITH latest_runs AS MATERIALIZED (
                SELECT DISTINCT ON (mi.river_network_version_id)
                       h.run_id, mi.river_network_version_id,
                       h.run_key, rnv.river_network_version_key
                FROM hydro.hydro_run h
                JOIN core.model_instance mi ON mi.basin_version_id = h.basin_version_id
                JOIN core.river_network_version rnv
                  ON rnv.river_network_version_id = mi.river_network_version_id
                JOIN hydro.run_display_coverage rdc
                  ON rdc.run_id = h.run_id
                 AND rdc.segment_count > 0
                 AND rdc.river_valid_time_start <= :valid_time
                 AND rdc.river_valid_time_end >= :valid_time
                WHERE h.status IN ('succeeded', 'parsed', 'published')
                  AND mi.river_network_version_id IS NOT NULL
                  AND mi.active_flag
                  -- #2007: the SAME identity pair as the source_identity_stats
                  -- probe above; NULL binds keep the legacy route unchanged.
                  AND (CAST(:source AS text) IS NULL OR lower(h.source_id) = :source)
                  AND (CAST(:cycle AS timestamptz) IS NULL OR h.cycle_time = :cycle)
                ORDER BY mi.river_network_version_id, h.cycle_time DESC, h.run_id DESC
            ),
            network_stream_max AS MATERIALIZED (
                SELECT lr.river_network_version_id,
                       MAX(rs0.stream_type) AS max_stream_type
                FROM latest_runs lr
                JOIN core.river_segment rs0
                  ON rs0.river_network_version_id = lr.river_network_version_id
                GROUP BY lr.river_network_version_id
            ),
            tile_segments AS MATERIALIZED (
                SELECT rs.river_segment_id,
                       rs.river_network_version_id,
                       rs.river_segment_key,
                       rs.stream_type
                FROM core.river_segment rs
                JOIN network_stream_max nsm
                  ON nsm.river_network_version_id = rs.river_network_version_id
                CROSS JOIN bounds
                WHERE rs.geom IS NOT NULL
                  AND rs.geom && ST_Transform(bounds.geom_3857, 4490)
                  AND (
                      :z >= 9
                      OR rs.stream_type IS NULL
                      OR rs.stream_type >= LEAST(
                          CASE
                              WHEN :z <= 4 THEN 5.0
                              WHEN :z = 5 THEN 4.0
                              WHEN :z = 6 THEN 3.0
                              WHEN :z = 7 THEN 2.0
                              ELSE 1.0
                          END,
                          nsm.max_stream_type
                      )
                  )
            ),
            -- typed_values and untyped_ranked are the SAME fact read under two
            -- zoom branches (z>=9 vs z<9). Issue #1341 switches BOTH to the
            -- surrogate keys in one step: leaving either leg on the old text
            -- predicates would make NULL-key legacy rows visible at one zoom
            -- and invisible at the other for one and the same national
            -- identity. The two legs therefore stay symmetric — same probe,
            -- same predicates, same fact columns — and only their zoom guard,
            -- projection tail and percent-rank window differ.
            --
            -- Both legs drive from tile_segments and probe the fact table once
            -- per segment through a LATERAL, instead of joining latest_runs to
            -- the fact table set-based and filtering the result against the
            -- segments. tile_segments is a spatial CTE the planner cannot
            -- estimate (node-27 z4: est ~50 rows vs 3,500 actual, and ANALYZE
            -- does not move it), so the set-based shape materialised the whole
            -- run slice and join-filtered it: 56,567 x 3,500 = 198M row
            -- comparisons, 34.7s against 0.77s for the pre-switch text query.
            -- The per-segment probe is 0.086ms/loop on uncompressed chunks and
            -- 0.013ms/loop through the compressed segmentby index.
            --   * LIMIT 1 is REQUIRED and semantically neutral: the fact
            --     PRIMARY KEY is (run_id, river_network_version_id,
            --     river_segment_id, variable, valid_time), all five of which
            --     the probe binds, so it can match at most one row anyway. Its
            --     job is to fence the subquery against planner pull-up, which
            --     is what keeps the probe from collapsing back into the join.
            --   * The lr <-> seg equality on river_network_version_id is
            --     semantics-preserving: a segment's fact rows only ever exist
            --     under its own network's run, so the edge can neither drop
            --     nor duplicate rows — it only stops the probe from running
            --     once per (segment, foreign network) pair.
            --   * The text predicates are transitional pushdown aids removed
            --     with #1342. Unlike in the set-based shape they are not dead
            --     weight here: inside the LATERAL the correlated lr./seg.
            --     values are constants for one probe, so they do reach the
            --     compressed-chunk segmentby index (segmentby run_id,
            --     river_network_version_id, river_segment_id) and the text
            --     primary key on uncompressed chunks. Each one sits in the
            --     same conjunction as its key counterpart, so it is a strict
            --     no-op on keyed rows.
            typed_values AS (
                SELECT seg.river_segment_id,
                       seg.river_segment_key,
                       seg.river_network_version_id,
                       v.basin_version_key,
                       v.value,
                       v.unit,
                       v.quality_flag,
                       lr.run_id,
                       v.variable,
                       v.valid_time
                FROM tile_segments seg
                JOIN latest_runs lr
                  ON lr.river_network_version_id = seg.river_network_version_id
                CROSS JOIN LATERAL (
                    SELECT ts.basin_version_key,
                           ts.value,
                           ts.unit_e::text AS unit,
                           ts.quality_flag_e::text AS quality_flag,
                           ts.variable_e::text AS variable,
                           ts.valid_time
                    FROM hydro.river_timeseries ts
                    WHERE ts.run_key = lr.run_key
                      AND ts.river_network_version_key = lr.river_network_version_key
                      AND ts.river_segment_key = seg.river_segment_key
                      -- These four aids do double duty: 000047's segmentby
                      -- pruning AND the text primary key's per-loop lookup.
                      -- The nuance lives here, in prose, because the removal
                      -- marker below is byte-frozen (#1342 deletes by that
                      -- exact line) and carries one aid each.
                      -- transitional compressed-chunk pushdown aid, remove with #1342
                      AND ts.run_id = lr.run_id
                      -- transitional compressed-chunk pushdown aid, remove with #1342
                      AND ts.river_network_version_id = lr.river_network_version_id
                      -- transitional compressed-chunk pushdown aid, remove with #1342
                      AND ts.river_segment_id = seg.river_segment_id
                      -- transitional compressed-chunk pushdown aid, remove with #1342
                      AND ts.variable = :variable
                      AND ts.variable_e = (
                              SELECT e FROM unnest(enum_range(NULL::hydro.river_variable)) e
                              WHERE e::text = :variable
                          )
                      AND ts.valid_time = :valid_time
                    LIMIT 1
                ) v
                WHERE (:z >= 9 OR seg.stream_type IS NOT NULL)
            ),
            untyped_ranked AS (
                SELECT seg.river_segment_id,
                       seg.river_segment_key,
                       seg.river_network_version_id,
                       v.basin_version_key,
                       v.value,
                       v.unit,
                       v.quality_flag,
                       lr.run_id,
                       v.variable,
                       v.valid_time,
                       CASE
                           WHEN v.value IS NULL THEN NULL
                           ELSE PERCENT_RANK() OVER (
                               PARTITION BY lr.river_network_version_key
                               ORDER BY v.value
                           )
                       END AS value_percent_rank
                FROM tile_segments seg
                JOIN latest_runs lr
                  ON lr.river_network_version_id = seg.river_network_version_id
                CROSS JOIN LATERAL (
                    SELECT ts.basin_version_key,
                           ts.value,
                           ts.unit_e::text AS unit,
                           ts.quality_flag_e::text AS quality_flag,
                           ts.variable_e::text AS variable,
                           ts.valid_time
                    FROM hydro.river_timeseries ts
                    WHERE ts.run_key = lr.run_key
                      AND ts.river_network_version_key = lr.river_network_version_key
                      AND ts.river_segment_key = seg.river_segment_key
                      -- These four aids do double duty: 000047's segmentby
                      -- pruning AND the text primary key's per-loop lookup.
                      -- The nuance lives here, in prose, because the removal
                      -- marker below is byte-frozen (#1342 deletes by that
                      -- exact line) and carries one aid each.
                      -- transitional compressed-chunk pushdown aid, remove with #1342
                      AND ts.run_id = lr.run_id
                      -- transitional compressed-chunk pushdown aid, remove with #1342
                      AND ts.river_network_version_id = lr.river_network_version_id
                      -- transitional compressed-chunk pushdown aid, remove with #1342
                      AND ts.river_segment_id = seg.river_segment_id
                      -- transitional compressed-chunk pushdown aid, remove with #1342
                      AND ts.variable = :variable
                      AND ts.variable_e = (
                              SELECT e FROM unnest(enum_range(NULL::hydro.river_variable)) e
                              WHERE e::text = :variable
                          )
                      AND ts.valid_time = :valid_time
                    LIMIT 1
                ) v
                WHERE :z < 9
                  AND seg.stream_type IS NULL
            ),
            selected_values AS (
                SELECT river_segment_id, river_segment_key, river_network_version_id,
                       basin_version_key,
                       value, unit, quality_flag, run_id, variable, valid_time
                FROM typed_values
                UNION ALL
                SELECT river_segment_id, river_segment_key, river_network_version_id,
                       basin_version_key,
                       value, unit, quality_flag, run_id, variable, valid_time
                FROM untyped_ranked
                WHERE value_percent_rank IS NOT NULL
                  AND value_percent_rank >= CASE
                      WHEN :z <= 4 THEN 0.90
                      WHEN :z = 5 THEN 0.70
                      WHEN :z = 6 THEN 0.40
                      WHEN :z = 7 THEN 0.15
                      ELSE 0.04
                  END
            )
            SELECT (sv.river_network_version_id || '::' || sv.river_segment_id) AS feature_id,
                   sv.river_segment_id AS segment_id,
                   sv.river_segment_id,
                   sv.river_network_version_id,
                   bv.basin_version_id,
                   bv.basin_id,
                   sv.value,
                   sv.unit,
                   sv.quality_flag,
                   sv.run_id,
                   sv.variable,
                   to_char(sv.valid_time AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS valid_time,
                   CASE
                       WHEN :z >= 9 THEN rs.geom
                       ELSE ST_Transform(
                           ST_SimplifyPreserveTopology(
                               ST_Transform(rs.geom, 3857),
                               CASE
                                   WHEN :z <= 4 THEN 2000.0
                                   WHEN :z = 5 THEN 1000.0
                                   WHEN :z = 6 THEN 500.0
                                   WHEN :z = 7 THEN 200.0
                                   ELSE 80.0
                               END
                           ),
                           4490
                       )
                   END AS geom
            FROM selected_values sv
            JOIN core.river_segment rs
              ON rs.river_segment_key = sv.river_segment_key
            -- basin_version_id now comes from this join too (issue #1341); it
            -- stays a LEFT JOIN so the row count is unchanged. A surviving row
            -- always has a non-NULL basin_version_key that the dual write
            -- resolved from this very table, and core.basin_version rows cannot
            -- be deleted while hydro_run / model_instance / river_network_version
            -- reference them, so the outer-join miss that would blank
            -- basin_version_id is not a reachable state.
            LEFT JOIN core.basin_version bv
              ON bv.basin_version_key = sv.basin_version_key
        
        ),
        source_identity_stats AS (
            
            SELECT CASE WHEN EXISTS (
                SELECT 1
                FROM (
                    SELECT DISTINCT ON (mi.river_network_version_id)
                           h.run_key, rnv.river_network_version_key,
                           h.run_id, mi.river_network_version_id
                    FROM hydro.hydro_run h
                    JOIN core.model_instance mi ON mi.basin_version_id = h.basin_version_id
                    JOIN core.river_network_version rnv
                      ON rnv.river_network_version_id = mi.river_network_version_id
                    JOIN hydro.run_display_coverage rdc
                      ON rdc.run_id = h.run_id
                     AND rdc.segment_count > 0
                     AND rdc.river_valid_time_start <= :valid_time
                     AND rdc.river_valid_time_end >= :valid_time
                    WHERE h.status IN ('succeeded', 'parsed', 'published')
                      AND mi.river_network_version_id IS NOT NULL
                      AND mi.active_flag
                      -- #2007: the SAME identity pair as the latest_runs CTE
                      -- below. This sub-select decides the route's 424, so a
                      -- divergence here answers "present" from another source.
                      AND (CAST(:source AS text) IS NULL OR lower(h.source_id) = :source)
                      AND (CAST(:cycle AS timestamptz) IS NULL OR h.cycle_time = :cycle)
                    ORDER BY mi.river_network_version_id, h.cycle_time DESC, h.run_id DESC
                ) lr
                CROSS JOIN LATERAL (
                    SELECT 1
                    FROM hydro.river_timeseries ts
                    WHERE ts.run_key = lr.run_key
                      AND ts.river_network_version_key = lr.river_network_version_key
                      -- transitional compressed-chunk pushdown aid, remove with #1342
                      AND ts.run_id = lr.run_id
                      -- transitional compressed-chunk pushdown aid, remove with #1342
                      AND ts.river_network_version_id = lr.river_network_version_id
                      -- transitional compressed-chunk pushdown aid, remove with #1342
                      AND ts.variable = :variable
                      AND ts.variable_e = (
                              SELECT e FROM unnest(enum_range(NULL::hydro.river_variable)) e
                              WHERE e::text = :variable
                          )
                      AND ts.valid_time = :valid_time
                    LIMIT 1
                ) hit
                LIMIT 1
            ) THEN 1 ELSE 0 END AS source_identity_count
        
        ),
        bounded_rows AS (
            SELECT source_rows.*,
                   ST_NPoints(source_rows.geom) AS source_coordinate_count,
                   ST_NDims(source_rows.geom) AS source_coordinate_dimensions
            FROM source_rows, bounds
            WHERE source_rows.geom IS NOT NULL
              AND source_rows.geom && ST_Transform(bounds.geom_3857, 4490)
        ),
        source_stats AS (
            SELECT CASE WHEN EXISTS (SELECT 1 FROM bounded_rows) THEN 1 ELSE 0 END AS source_feature_count
        ),
        
        preeligible AS (
            SELECT *
            FROM bounded_rows
            WHERE source_coordinate_count <= :feature_coordinate_limit
              AND source_coordinate_dimensions <= :max_coordinate_dimensions
        ),
        -- The low-zoom stream/rank filter above deliberately keeps every
        -- selected local trunk. A nationwide tile can still cross the shared
        -- MVT budget after a cohort expands. Bound it here instead of turning
        -- a renderable overview tile into HTTP 413. `network_rank` interleaves
        -- each network's strongest remaining segment before its second one, so
        -- a large basin cannot evict every smaller basin from the same tile.
        national_ranked AS (
            SELECT preeligible.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY river_network_version_id
                       ORDER BY value DESC NULLS LAST, river_segment_id
                   ) AS network_rank
            FROM preeligible
        ),
        national_budget_window AS (
            SELECT national_ranked.*,
                   ROW_NUMBER() OVER (
                       ORDER BY network_rank, value DESC NULLS LAST,
                                river_network_version_id, river_segment_id
                   ) AS tile_feature_rank,
                   SUM(source_coordinate_count) OVER (
                       ORDER BY network_rank, value DESC NULLS LAST,
                                river_network_version_id, river_segment_id
                       ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                   ) AS tile_coordinate_rank
            FROM national_ranked
        ),
        eligible AS (
            SELECT *
            FROM national_budget_window
            WHERE tile_feature_rank <= :feature_limit
              AND tile_coordinate_rank <= :collection_coordinate_limit
        )
        ,
        prefilter_stats AS (
            SELECT COUNT(*) AS intersecting_feature_count,
                   COALESCE(SUM(source_coordinate_count), 0) AS intersecting_coordinate_count,
                   COALESCE(MAX(source_coordinate_count), 0) AS feature_coordinate_count,
                   COUNT(*) FILTER (
                       WHERE source_coordinate_count > :feature_coordinate_limit
                   ) AS feature_coordinate_overflow_count,
                   COALESCE(MAX(source_coordinate_dimensions), 0) AS coordinate_dimension_count,
                   COUNT(*) FILTER (
                       WHERE source_coordinate_dimensions > :max_coordinate_dimensions
                   ) AS coordinate_dimension_overflow_count,
                   COUNT(*) FILTER (WHERE feature_id IS NULL OR feature_id::text = '') + COUNT(*) FILTER (WHERE segment_id IS NULL OR segment_id::text = '') + COUNT(*) FILTER (WHERE river_segment_id IS NULL OR river_segment_id::text = '') + COUNT(*) FILTER (WHERE river_network_version_id IS NULL OR river_network_version_id::text = '') + COUNT(*) FILTER (WHERE basin_version_id IS NULL OR basin_version_id::text = '') + COUNT(*) FILTER (WHERE value IS NULL OR value::double precision IN ('NaN'::double precision, 'Infinity'::double precision, '-Infinity'::double precision)) + COUNT(*) FILTER (WHERE unit IS NULL OR unit::text = '') + COUNT(*) FILTER (WHERE quality_flag IS NULL OR quality_flag::text = '') + COUNT(*) FILTER (WHERE run_id IS NULL OR run_id::text = '') + COUNT(*) FILTER (WHERE variable IS NULL OR variable::text = '') + COUNT(*) FILTER (WHERE valid_time IS NULL) AS invalid_property_count,
                   concat_ws(',',
                   CASE WHEN COUNT(*) FILTER (WHERE feature_id IS NULL OR feature_id::text = '') > 0 THEN 'feature_id' END,
                   CASE WHEN COUNT(*) FILTER (WHERE segment_id IS NULL OR segment_id::text = '') > 0 THEN 'segment_id' END,
                   CASE WHEN COUNT(*) FILTER (WHERE river_segment_id IS NULL OR river_segment_id::text = '') > 0 THEN 'river_segment_id' END,
                   CASE WHEN COUNT(*) FILTER (WHERE river_network_version_id IS NULL OR river_network_version_id::text = '') > 0 THEN 'river_network_version_id' END,
                   CASE WHEN COUNT(*) FILTER (WHERE basin_version_id IS NULL OR basin_version_id::text = '') > 0 THEN 'basin_version_id' END,
                   CASE WHEN COUNT(*) FILTER (WHERE value IS NULL OR value::double precision IN ('NaN'::double precision, 'Infinity'::double precision, '-Infinity'::double precision)) > 0 THEN 'value' END,
                   CASE WHEN COUNT(*) FILTER (WHERE unit IS NULL OR unit::text = '') > 0 THEN 'unit' END,
                   CASE WHEN COUNT(*) FILTER (WHERE quality_flag IS NULL OR quality_flag::text = '') > 0 THEN 'quality_flag' END,
                   CASE WHEN COUNT(*) FILTER (WHERE run_id IS NULL OR run_id::text = '') > 0 THEN 'run_id' END,
                   CASE WHEN COUNT(*) FILTER (WHERE variable IS NULL OR variable::text = '') > 0 THEN 'variable' END,
                   CASE WHEN COUNT(*) FILTER (WHERE valid_time IS NULL) > 0 THEN 'valid_time' END
                   ) AS invalid_properties
            FROM bounded_rows
        ),
        budget_stats AS (
            SELECT COUNT(*) AS feature_count,
                   COALESCE(SUM(source_coordinate_count), 0) AS coordinate_count
            FROM eligible
        ),
        budget_gate AS (
            SELECT budget_stats.feature_count, budget_stats.coordinate_count
            FROM budget_stats, prefilter_stats
            WHERE budget_stats.feature_count <= :feature_limit
              AND budget_stats.coordinate_count <= :collection_coordinate_limit
              AND prefilter_stats.feature_coordinate_overflow_count = 0
              AND prefilter_stats.coordinate_dimension_overflow_count = 0
              AND prefilter_stats.invalid_property_count = 0
        ),
        simplified AS (
            SELECT eligible.*,
                   ST_SimplifyPreserveTopology(
                       ST_MakeValid(ST_Transform(eligible.geom, 3857)),
                       :simplification_tolerance_m
                   ) AS geom_3857
            FROM eligible
            CROSS JOIN budget_gate
        ),
        clipped AS (
            SELECT simplified.*,
                   ST_AsMVTGeom(
                       simplified.geom_3857,
                       bounds.geom_3857,
                       extent => 4096,
                       buffer => 64,
                       clip_geom => true
                   ) AS mvt_geom
            FROM simplified, bounds
        ),
        budgeted AS (
            SELECT clipped.*,
                   budget_gate.feature_count,
                   budget_gate.coordinate_count
            FROM clipped
            CROSS JOIN budget_gate
            WHERE mvt_geom IS NOT NULL
        )
        SELECT (
            SELECT ST_AsMVT(tile_rows, 'hydro', 4096, 'mvt_geom')
            FROM (
                SELECT feature_id,
                       segment_id,
                       river_segment_id,
                       river_network_version_id,
                       basin_version_id,
                       basin_id,
                       value,
                       unit,
                       quality_flag,
                       run_id,
                       variable,
                       valid_time,
                       mvt_geom
                FROM budgeted
                ORDER BY river_network_version_id, river_segment_id
            ) AS tile_rows
        ) AS tile,
        (SELECT source_identity_count FROM source_identity_stats) AS source_identity_count,
        (SELECT source_feature_count FROM source_stats) AS source_feature_count,
        (SELECT feature_count FROM budget_stats) AS feature_count,
        (SELECT coordinate_count FROM budget_stats) AS coordinate_count,
        (SELECT intersecting_feature_count FROM prefilter_stats) AS intersecting_feature_count,
        (SELECT intersecting_coordinate_count FROM prefilter_stats) AS intersecting_coordinate_count,
        (SELECT feature_coordinate_overflow_count FROM prefilter_stats) AS feature_coordinate_overflow_count,
        (SELECT feature_coordinate_count FROM prefilter_stats) AS feature_coordinate_count,
        (SELECT coordinate_dimension_overflow_count FROM prefilter_stats) AS coordinate_dimension_overflow_count,
        (SELECT coordinate_dimension_count FROM prefilter_stats) AS coordinate_dimension_count,
        (SELECT invalid_property_count FROM prefilter_stats) AS invalid_property_count,
        (SELECT invalid_properties FROM prefilter_stats) AS invalid_properties
        FROM source_identity_stats, source_stats, budget_stats, prefilter_stats
    