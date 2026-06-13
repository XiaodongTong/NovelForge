# NovelForge — Agent & Contributor Guide

> Repository constitution for AI agents and human contributors working on this codebase.

## Project Overview

NovelForge is a **declarative, pipeline-driven AI long-form novel engine** built in Python 3.10+. Authors provide a `novel-project.yaml` with story seeds, and the engine autonomously drives a multi-stage FSM pipeline (outline → characters → chapters → review → polish) via Claude Code CLI.

## Repository Structure

```
novelforge/
├── src/novelforge/          # Engine source (Python package)
│   ├── cli.py               # Typer CLI entry point
│   ├── config.py            # YAML config loading + validation
│   ├── state.py             # StateStore, Checkpoint, RecoveryPlan
│   ├── orchestrator.py      # FSM pipeline driver
│   ├── errors.py            # Exception hierarchy
│   ├── stages/              # GenericStage (v4) + v3 stage-class compatibility shims
│   ├── claude/              # Claude adapter, context manager, token logging
│   ├── review/              # Review gate + JSON schema
│   └── utils/               # Filesystem helpers, logging config
├── tests/                   # pytest test suite
├── samples/minimal-novel/   # Minimal sample project (1 chapter, mock-runnable)
├── docs/plan/               # Design documentation
├── .cybervisor/             # AI team orchestration metadata (not engine state)
├── pyproject.toml           # Package definition, deps, entry points
└── README.md                # User-facing documentation
```

## Key Boundaries

| Directory | Purpose | Who writes it |
|-----------|---------|---------------|
| `.cybervisor/` | AI development orchestration (design → review → implement → verify) | cybervisor AI team |
| `.novelforge/` (inside a user project) | Engine runtime state (state.yaml, checkpoints, logs) | NovelForge engine at runtime |
| `samples/minimal-novel/` | Reference sample project for testing | Maintained by contributors |

These directories **must never read from or write to each other**.

## Development Workflow

### Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[test]"
```

### Running Tests

```bash
pytest -q                                          # all tests
pytest --cov=novelforge --cov-report=term-missing  # with coverage
pytest tests/test_config.py -v                     # single file
```

Coverage threshold: **80% line coverage** on core modules (config, state, context, review schema, orchestrator FSM, error recovery). The CI enforces this via `pyproject.toml` `[tool.coverage.report] fail_under`.

### Mock Mode

All end-to-end tests use `MockClaudeAdapter` — no real API calls. Use `--use-mock` or `NOVELFORGE_MOCK=1` to run the engine offline.

## Coding Conventions

- **Type hints**: mandatory on all public functions and dataclass fields
- **Dataclasses**: use `frozen=True` for config/spec types; mutable state types (e.g. `State`) are not frozen
- **Logging**: always use `get_logger("module_name")` from `utils.log`; never use `print` for engine output
- **File writes**: always go through `utils.fs.atomic_write` (write `.tmp` → `fsync` → `os.replace`)
- **Error handling**: raise domain-specific exceptions from `errors.py`; catch them in the orchestrator, never silently swallow
- **Stage interface**: every stage is a `Stage` subclass in `src/novelforge/stages/` with an `execute(ctx) -> StageExecutionResult` method. In v4, the runtime drives every step through `GenericStage` (see `stages/generic.py`); per-stage behaviour is declared in `novel-project.yaml` as an 8-field `StageConfig` record (see `config.py` → `StageConfig`). The 10 v3 stage classes under `stages/` are kept as deprecated compatibility shims for `template:` / `stages_override:` yaml and are scheduled for removal.

## Adding a New Pipeline Stage

v4 separates **what a stage does** (declared in yaml) from **how the engine runs it** (`GenericStage`). The right path depends on whether the new stage is project-specific or built-in:

**Project-specific stage (most common):** edit the project's `novel-project.yaml` and append a new entry under `pipeline.stages` with the 8 fields (`id / model / prompt / output / split? / batch? / on_failure? / enabled?`). No Python change is required — the engine will pick it up on the next `novelforge run` / `validate`.

**New built-in template stage (scaffolds via `init` / `migrate`):**

1. Add the stage's defaults to `src/novelforge/templates.py` as a `StageTemplate` dataclass and register it in `ALL_STAGE_IDS` + the relevant entry of `PIPELINE_TEMPLATES`.
2. Add a prompt body in `src/novelforge/templates.py` (`prompt_text`) — `init` will materialise it to `prompts/<prompt_file>.md` in the user's project.
3. Write tests in `tests/` (parser / `GenericStage` matrix tests cover new stages automatically once the template record is in place).

Do **not** create new files under `src/novelforge/stages/` or wire entries into `build_stage_registry()` — that is the v3 path and is being phased out per `docs/plan/pipeline-customization.md`.

## CI & Quality Gates

- All tests must pass (`pytest -q`)
- Coverage ≥ 80% on core modules
- Config validation: `novelforge validate` must exit 0 on the sample project
- The `cybervisor.yaml` drives a 4-stage AI development pipeline (design → review → implement → verify) — this is separate from the novel pipeline

## Common Pitfalls

- Do not place engine state files under `.cybervisor/` or vice versa
- The `metrics/` directory inside `.novelforge/` is created at runtime but no quality-scores file is auto-written in v4; the dir is reserved for future metric extensions
- The `final_polish` stage can be skipped via `--skip-polish`; do not assume it always runs
- Token usage parsing from Claude CLI stdout is best-effort; failures log a warning but do not block the pipeline
- v3 yaml (only `pipeline.template:` / `pipeline.stages_override:`) is still loadable but emits a `DeprecationWarning` at run time and is not the recommended path. New work should target the v4 `pipeline.stages: [...]` form; see `docs/plan/pipeline-customization.md`
- `pipeline.scaffold_from:` is a metadata-only field — the runtime ignores it (including unknown template names) and no warning is emitted
