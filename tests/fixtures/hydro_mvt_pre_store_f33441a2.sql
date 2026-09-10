
        WITH bounds AS (
            SELECT ST_TileEnvelope(:z, :x, :y) AS geom_3857
        ),
        source_rows AS NOT MATERIALIZED (
            
            SELECT ((:river_network_version_id)::text || '::' || rs.river_segment_id) AS feature_id,
                   rs.river_segment_id AS segment_id,
                   rs.river_segment_id,
                   (:river_network_version_id)::text AS river_network_version_id,
                   (:basin_version_id)::text AS basin_version_id,
                   ts.value, ts.unit_e::text AS unit,
                   ts.quality_flag_e::text AS quality_flag,
                   (:run_id)::text AS run_id, ts.variable_e::text AS variable,
                   to_char(ts.valid_time AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS valid_time,
                   rs.geom
            FROM hydro.river_timeseries ts
            JOIN core.river_segment rs
              ON rs.river_segment_key = ts.river_segment_key
            WHERE ts.run_key = (
                      SELECT run_key FROM hydro.hydro_run WHERE run_id = :run_id
                  )
              -- transitional compressed-chunk pushdown aid, remove with #1342
              AND ts.run_id = :run_id
              AND ts.basin_version_key = (
                      SELECT basin_version_key FROM core.basin_version
                      WHERE basin_version_id = :basin_version_id
                  )
              -- transitional compressed-chunk pushdown aid, remove with #1342
              AND ts.river_network_version_id = :river_network_version_id
              AND ts.river_network_version_key = (
                      SELECT river_network_version_key FROM core.river_network_version
                      WHERE river_network_version_id = :river_network_version_id
                  )
              -- transitional compressed-chunk pushdown aid, remove with #1342
              AND ts.variable = :variable
              AND ts.variable_e = (
                      SELECT e FROM unnest(enum_range(NULL::hydro.river_variable)) e
                      WHERE e::text = :variable
                  )
              AND ts.valid_time = :valid_time
        
        ),
        source_identity_stats AS (
            SELECT CASE WHEN EXISTS (SELECT 1 FROM source_rows) THEN 1 ELSE 0 END AS source_identity_count
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
        
        eligible AS (
            SELECT *
            FROM bounded_rows
            WHERE source_coordinate_count <= :feature_coordinate_limit
              AND source_coordinate_dimensions <= :max_coordinate_dimensions
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
    