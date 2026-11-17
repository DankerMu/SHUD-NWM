# Tasks — selector-route-shud-preflight (#3167)

## Risk packs

| Pack | Selected | Reason / mapping |
|---|---|---|
| CI routing / test selection | yes | new PathTestRule and its pin: E2, E3 |
| Others | no | no production code, schema, config or runtime change |

- [x] 1.1 Add the `packages/common/shud_preflight.py` PathTestRule (proposal What 1).
- [x] 1.2 Add a routing pin in `tests/test_select_ci_tests.py` that also asserts `CORE_SMOKE_TESTS ⊆ selection`, shown
  red before 1.1.
- [x] 1.3 Record the "does not join `GUARDED_MODULE_CLOSURES`" decision (proposal What 3) in a comment beside the
  `hydro_display_*` precedent (~:6215-6221), with the one-hop count measured by the guard's own helpers.
- [x] 1.4 Run E1–E3.
