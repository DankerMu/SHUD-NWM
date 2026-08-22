# national-geo-basin-discovery Spec Delta

## ADDED Requirements

### Requirement: National geo builders derive basin identity from container depth

The national domain/river GeoJSON builders SHALL derive each basin name from the position of the
`input/` segment in the shapefile path relative to the Basins root, never from a hard-coded
directory name. A model whose `input/` sits one level below the root SHALL take the root-level
directory name; a model whose `input/` sits two levels below SHALL take `<container>_<child-lowercased>`;
a model nested deeper SHALL be skipped rather than collapsed onto its top-level directory name.
This mirrors the depth-1/depth-2 rule already implemented by `basins_discovery._find_model_dirs`.

#### Scenario: Renamed container directory keeps per-child basin identity

- **WHEN** the national domain or river builder discovers `HYS/BST/input/BST/gis/domain.shp` and
  `HYS/MC/input/MC/gis/domain.shp` under the Basins root
- **THEN** it yields two distinct basins named `HYS_bst` and `HYS_mc` — rather than collapsing both onto
  the container name `HYS` and emitting one basin id that silently overwrites the other — and a model
  nested three or more levels above its `input/` segment is skipped instead of being attributed to its
  top-level directory
