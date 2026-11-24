## ADDED Requirements

### Requirement: Scheduler owner modules SHALL NOT copy another module's namespace

No module under `services/orchestrator` SHALL copy another module's namespace into its own with a
`globals().update(...)` call. An owner module
that binds its names this way hides what it depends on and takes over the other module's identity attributes.
For `services.orchestrator.scheduler_candidate_runtime`, which used such a copy of the scheduler facade:
- **Identity:** its `__name__`, `__file__` and `__spec__` SHALL be its own, and every function defined in it SHALL
  report it as `__module__`.
- **Explicit imports:** it SHALL import each name it uses at run time from a module that binds the same object the
  facade binds, and SHALL hold no facade binding. Its source SHALL carry no exemption from the undefined-name and
  unused-import lint rules, neither on a `noqa` line nor in the project's per-file ignores.
- **Facade surface:** the names the scheduler facade exposes SHALL NOT change because of the switch.
- **Patches:** a patch on a facade name is not observed inside `scheduler_candidate_runtime`, as it was not under the
  import-time copy.

This requirement does not change how other owner modules read facade names at call time through a facade binding.

#### Scenario: The candidate-runtime owner keeps its own identity

- **WHEN** `services.orchestrator.scheduler_candidate_runtime` is imported after the scheduler facade
- **THEN** its `__name__` is `services.orchestrator.scheduler_candidate_runtime`, its `__file__` is its own source
  file, and every function defined in it reports that module name

#### Scenario: A namespace copy is rejected before merge

- **WHEN** any source file under `services/orchestrator` contains a `globals().update(...)` call
- **THEN** a test fails

#### Scenario: The run-time names are the facade's objects

- **WHEN** the scheduler facade and `scheduler_candidate_runtime` are both imported
- **THEN** each name the module imports for run-time use is the same object as the facade's attribute of that name,
  and every name the facade imports from the module is the module's own attribute
