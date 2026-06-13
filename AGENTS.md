# NovelForge — Agent & Contributor Guide

> Repository constitution for AI agents and human contributors working on this codebase.

## Project Overview

NovelForge is a **declarative, pipeline-driven AI long-form novel engine** built in Python 3.10+. Authors provide a `novel-project.yaml` with story seeds, and the engine autonomously drives a contract pipeline (outline → characters → chapters → review → polish) via Claude Code CLI.

The pipeline model is the **stage contract**: every stage declares its outputs (`produces`), its completion criteria (`done_when`), and the upstream stages it consumes (`consumes`).  Data flows through an in-memory `ArtifactRegistry` keyed by `(stage_id, alias)`; downstream stages reference upstream outputs via the `{{upstream.<id>.<alias>}}` placeholder family.

## Repository Structure

```
novelforge/
├── src/novelforge/              # Engine source (Python package)
│   ├── cli.py                   # Typer CLI entry point (run/resume/status/validate/init)
│   ├── config.py                # YAML loading + StageConfig / ProduceSpec parsing
│   ├── state.py                 # StateStore, Checkpoint, RecoveryPlan
│   ├── orchestrator.py          # Dual-layer retry + batch driver
│   ├── errors.py                # Four-tier exception hierarchy
│   ├── artifact_registry.py     # Runtime {stage_id: {alias: Path | list[Path]}} store
│   ├── verify.py                # CheckSpec / DoneWhenSpec + 6 check kinds
│   ├── stages/                  # GenericStage (single executor for every step)
│   ├── claude/                  # Adapter, context manager, output parser, token logging
│   └── utils/                   # Filesystem helpers, logging config
├── tests/                       # pytest test suite
├── samples/minimal-novel/       # Reference sample project (mock-runnable)
├── docs/plan/                   # Design documentation
│   └── stage-contract.md        # Authoritative description of the contract model
├── .cybervisor/                 # AI team orchestration metadata (not engine state)
├── pyproject.toml               # Package definition, deps, entry points
└── README.md                    # User-facing documentation
```

## Key Boundaries

| Directory | Purpose | Who writes it |
|-----------|---------|---------------|
| `.cybervisor/` | AI development orchestration (design → review → implement → verify) | cybervisor AI team |
| `.novelforge/` (inside a user project) | Engine runtime state (`state.yaml`, checkpoints, logs, attempts, registry snapshot) | NovelForge engine at runtime |
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

Coverage threshold: **80% line coverage** on core modules (config, state, context, verify, orchestrator, error recovery).  The CI enforces this via `pyproject.toml` `[tool.coverage.report] fail_under`.

### Mock Mode

All end-to-end tests use `MockClaudeAdapter` — no real API calls.  Use `--use-mock` or `NOVELFORGE_MOCK=1` to run the engine offline.

Three environment switches drive the negative scenarios in `tests/test_e2e_contract.py`:

- `NOVELFORGE_MOCK_NO_SIGNAL=1` — first invoke per `(stage, batch)` omits the completion signal (triggers `StageIncomplete`).
- `NOVELFORGE_MOCK_EMPTY=1`     — first invoke writes empty produces (triggers `VerifyFailed`).
- `NOVELFORGE_MOCK_ALWAYS_FAIL=1` — every invoke both omits the signal and writes empty produces.

## Coding Conventions

- **Type hints**: mandatory on all public functions and dataclass fields
- **Dataclasses**: use `frozen=True` for config/spec types; mutable state types (e.g. `State`) are not frozen
- **Logging**: always use `get_logger("module_name")` from `utils.log`; never use `print` for engine output
- **File writes**: always go through `utils.fs.atomic_write` (write `.tmp` → `fsync` → `os.replace`)
- **Error handling**: raise domain-specific exceptions from `errors.py`; catch them in the orchestrator, never silently swallow
- **Stage interface**: every stage is driven by the single `GenericStage` class (`src/novelforge/stages/generic.py`).  Per-step behaviour is declared in `novel-project.yaml` as a `StageConfig` record (`config.py`).  **Do not** add new files under `src/novelforge/stages/`; the v3 per-stage classes have been removed.

## Adding a New Pipeline Stage

v4 separates **what a stage does** (declared in yaml) from **how the engine runs it** (`GenericStage`).  The right path depends on whether the new stage is project-specific or built-in:

**Project-specific stage (most common):** edit the project's `novel-project.yaml` and append a new entry under `pipeline.stages` with the contract fields (`id / model / prompt / produces / done_when / consumes? / batch? / on_failure? / enabled?`).  No Python change is required — the engine will pick it up on the next `novelforge run` / `validate`.

**New built-in template stage (scaffolds via `init`):**

1. Add a `StageConfig(...)` builder function in `src/novelforge/templates.py` and append it to a `ContractTemplate` in `BUILTIN_TEMPLATES`.
2. Add a prompt body keyed by the stage id in the template's `prompts=` mapping — `init` will materialise it to `prompts/<prompt_file>.md` in the user's project.
3. Write tests in `tests/test_templates.py` to cover the new stage's produces + done_when shape.

Do **not** create new files under `src/novelforge/stages/` — that is the v3 path and has been removed.

## CI & Quality Gates

- All tests must pass (`pytest -q`)
- Coverage ≥ 80% on core modules
- Config validation: `novelforge validate --config samples/minimal-novel/novel-project.yaml` must exit 0
- The `cybervisor.yaml` drives a 4-stage AI development pipeline (design → review → implement → verify) — this is separate from the novel pipeline

## Common Pitfalls

- Do not place engine state files under `.cybervisor/` or vice versa
- The `metrics/` directory inside `.novelforge/` is reserved for future metric extensions
- Token usage parsing from Claude CLI stdout is best-effort; failures log a warning but do not block the pipeline
- v3 yaml (`pipeline.template:`, `pipeline.stages_override:`, `pipeline.scaffold_from:`) is **rejected at load time** (spec §AC-15).  Use `pipeline.stages: [...]` exclusively.
- The runtime is purely linear — there are no `NEEDS_REWRITE` / `FUNDAMENTAL_ISSUE` / `APPROVED` route tokens; the next stage is always the next entry in `pipeline.stages`.
