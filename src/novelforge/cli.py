"""Command-line entry point for the NovelForge engine.

Implemented with Typer.  Four subcommands are registered per ``spec.md``
§6 / ``tasks.md`` T0.3:

- ``novelforge run``     — start a fresh end-to-end pipeline run
- ``novelforge resume``  — continue from the last valid checkpoint
- ``novelforge status``  — print current state, progress, and token usage
- ``novelforge validate``— validate a ``novel-project.yaml`` configuration

The CLI is intentionally thin: argument parsing and exit codes live here,
business logic lives in the ``config``/``state``/``orchestrator`` modules.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping, Optional

import typer
from rich.console import Console

from . import __version__
from .config import ConfigError, load_config
from .orchestrator import Orchestrator
from .state import StateStore
from .utils.log import configure_logging, env_flag, get_logger

app = typer.Typer(
    name="novelforge",
    help="Declarative, pipeline-driven AI long-form novel engine.",
    no_args_is_help=True,
    add_completion=False,
)

console = Console(stderr=False)
err_console = Console(stderr=True)
log = get_logger("cli")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"novelforge {__version__}")
        raise typer.Exit()


@app.callback()
def main_root(
    version: Optional[bool] = typer.Option(
        None,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Print the engine version and exit.",
    ),
) -> None:
    """NovelForge engine entry point."""


def _resolve_path(config: Path) -> Path:
    return config.expanduser().resolve()


@app.command()
def validate(
    config: Path = typer.Option(
        ...,
        "--config",
        "-c",
        help="Path to novel-project.yaml.",
        exists=False,
    ),
) -> None:
    """Validate a novel-project.yaml file (does not run the pipeline)."""

    path = _resolve_path(config)
    if not path.exists():
        err_console.print(f"[red]Config file not found:[/red] {path}")
        raise typer.Exit(code=2)
    try:
        cfg = load_config(path)
    except ConfigError as exc:
        err_console.print(f"[red]Config validation failed:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except Exception as exc:  # SchemaInvalid and friends
        err_console.print(f"[red]Config validation failed:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    from .config import deprecation_warnings_for, stage_ids_for

    console.print(
        f"[green]Config OK:[/green] {cfg.novel.target_chapters} chapter(s)"
    )
    console.print(f"  novel      : {cfg.novel.title!r} ({cfg.novel.genre})")
    console.print(f"  stages     : {' -> '.join(stage_ids_for(cfg))}")
    if cfg.pipeline.template:
        console.print(
            f"  template   : {cfg.pipeline.template} (deprecated v3 form; "
            f"use pipeline.stages instead)"
        )
    if cfg.pipeline.scaffold_from:
        console.print(
            f"  scaffold_from: {cfg.pipeline.scaffold_from} (metadata only)"
        )
    if cfg.novel.words_per_chapter:
        console.print(
            f"  word budget: {cfg.novel.words_per_chapter[0]}-{cfg.novel.words_per_chapter[1]} chars/chapter"
        )

    # Surface deprecation warnings (A1) on validate too.
    for msg in deprecation_warnings_for(cfg):
        console.print(f"  [yellow]DeprecationWarning:[/yellow] {msg}")


def _build_orchestrator(path: Path, use_mock: bool) -> Orchestrator:
    cfg = load_config(path)
    return Orchestrator(
        config=cfg,
        config_path=path,
        project_root=path.parent,
        use_mock=use_mock,
    )


@app.command()
def run(
    config: Path = typer.Option(
        ...,
        "--config",
        "-c",
        help="Path to novel-project.yaml.",
    ),
    max_chapters: Optional[int] = typer.Option(
        None,
        "--max-chapters",
        help="Debug override for target_chapters (does not edit yaml).",
    ),
    skip_polish: bool = typer.Option(
        False,
        "--skip-polish",
        help="Skip the final_polish stage (useful for samples).",
    ),
    use_mock: bool = typer.Option(
        env_flag("NOVELFORGE_MOCK"),
        "--use-mock/--no-mock",
        help="Use the mock Claude adapter (overrides Claude Code CLI).",
    ),
) -> None:
    """Start a fresh end-to-end pipeline run."""

    try:
        path = _resolve_path(config)
        cfg = load_config(path)
    except ConfigError as exc:
        err_console.print(f"[red]Config validation failed:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except FileNotFoundError as exc:
        err_console.print(f"[red]Config file not found:[/red] {exc.filename}")
        raise typer.Exit(code=2) from exc

    project_root = path.parent
    state_dir = project_root / ".novelforge"
    configure_logging(
        level="INFO",
        log_dir=state_dir / "logs",
        console=not env_flag("NOVELFORGE_QUIET"),
    )

    orch = Orchestrator(
        config=cfg,
        config_path=path,
        project_root=project_root,
        use_mock=use_mock,
        max_chapters_override=max_chapters,
        skip_polish=skip_polish,
    )
    summary = orch.run(fresh=True)
    _print_run_summary(summary)
    if summary.get("paused"):
        raise typer.Exit(code=3)
    if not summary.get("ok"):
        raise typer.Exit(code=1)


@app.command()
def resume(
    config: Path = typer.Option(
        ...,
        "--config",
        "-c",
        help="Path to novel-project.yaml.",
    ),
    force_stage: Optional[str] = typer.Option(
        None,
        "--force-stage",
        help="Skip recovery_plan and start from the named stage.",
    ),
    use_mock: bool = typer.Option(
        env_flag("NOVELFORGE_MOCK"),
        "--use-mock/--no-mock",
        help="Use the mock Claude adapter (overrides Claude Code CLI).",
    ),
) -> None:
    """Resume from the last valid checkpoint."""

    try:
        path = _resolve_path(config)
        cfg = load_config(path)
    except ConfigError as exc:
        err_console.print(f"[red]Config validation failed:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except FileNotFoundError as exc:
        err_console.print(f"[red]Config file not found:[/red] {exc.filename}")
        raise typer.Exit(code=2) from exc

    project_root = path.parent
    state_dir = project_root / ".novelforge"
    configure_logging(
        level="INFO",
        log_dir=state_dir / "logs",
        console=not env_flag("NOVELFORGE_QUIET"),
    )

    orch = Orchestrator(
        config=cfg,
        config_path=path,
        project_root=project_root,
        use_mock=use_mock,
    )
    summary = orch.run(fresh=False, force_stage=force_stage)
    _print_run_summary(summary)
    if summary.get("paused"):
        raise typer.Exit(code=3)
    if not summary.get("ok"):
        raise typer.Exit(code=1)


@app.command()
def status(
    config: Path = typer.Option(
        ...,
        "--config",
        "-c",
        help="Path to novel-project.yaml.",
    ),
) -> None:
    """Print current state, progress, and recent checkpoint info."""

    try:
        path = _resolve_path(config)
        cfg = load_config(path)
    except ConfigError as exc:
        err_console.print(f"[red]Config validation failed:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except FileNotFoundError as exc:
        err_console.print(f"[red]Config file not found:[/red] {exc.filename}")
        raise typer.Exit(code=2) from exc

    project_root = path.parent
    state_dir = project_root / ".novelforge"
    state_path = state_dir / "state.yaml"
    if not state_path.exists():
        console.print(
            f"[yellow]No state.yaml found.[/yellow] Run "
            f"`novelforge run --config {path}` first."
        )
        return

    state = StateStore(state_dir).load()
    snapshot = state.snapshot()
    progress = snapshot.get("progress", {}) or {}
    token_usage = snapshot.get("token_usage", {}) or {}
    recovery = snapshot.get("recovery", {}) or {}

    target = cfg.novel.target_chapters
    payload = {
        "current_stage": snapshot.get("current_stage"),
        "target_chapters": target,
        "progress": {
            "chapters_written": int(progress.get("chapters_written", 0) or 0),
            "chapters_reviewed": int(progress.get("chapters_reviewed", 0) or 0),
            "total_words": int(progress.get("total_words", 0) or 0),
            "outline": progress.get("outline", "pending"),
            "characters": progress.get("characters", "pending"),
            "simulation": progress.get("simulation", "pending"),
        },
        "token_usage": {
            "total_input": int(token_usage.get("total_input", 0) or 0),
            "total_output": int(token_usage.get("total_output", 0) or 0),
        },
        "last_review_iterations": snapshot.get("last_review_iterations", 0),
        "last_checkpoint_at": snapshot.get("last_checkpoint_at"),
        "paused": bool(snapshot.get("paused", False)),
        "paused_reason": snapshot.get("paused_reason"),
        "recovery": recovery,
        "started_at": snapshot.get("started_at"),
    }
    console.print_json(data=payload)


def _print_run_summary(summary: dict) -> None:
    if not summary:
        return
    if summary.get("ok"):
        console.print(
            f"[green]Pipeline finished[/green] "
            f"(stages_run={summary.get('stages_run', 0)}, "
            f"chapters_written={summary.get('chapters_written', 0)}, "
            f"total_tokens={summary.get('total_tokens', 0)})."
        )
    elif summary.get("paused"):
        reason = summary.get("paused_reason") or "unknown"
        console.print(
            f"[yellow]Pipeline paused[/yellow] reason={reason!r}. "
            f"Fix the underlying issue then run `novelforge resume`."
        )
    else:
        console.print(
            f"[red]Pipeline exited with status[/red] {summary.get('status')}."
        )


def main() -> None:
    """Console-script entry point defined in ``pyproject.toml``."""

    try:
        app()
    except KeyboardInterrupt:
        err_console.print("\n[yellow]Interrupted.[/yellow]")
        sys.exit(130)


# --------------------------------------------------------------------------- #
# v4 commands: init / migrate
# --------------------------------------------------------------------------- #


import yaml as _yaml  # local import: top of file already has `import yaml`
from . import templates as _templates
from .utils.fs import atomic_write as _atomic_write, ensure_dir as _ensure_dir


_DEFAULT_NOVEL_BLOCK = """\
title: "My Novel"
genre: "玄幻修仙"
target_chapters: 300
words_per_chapter: [2500, 3000]
style: "天蚕土豆、辰东"
seeds:
  - outline/premise.md
  - outline/world.md
constraints:
  - CLAUDE.md
"""


def _render_v4_yaml(
    *,
    template_name: str,
    novel_block: Optional[str] = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Return a v4 yaml payload (as a dict) + the prompts to materialise.

    ``novel_block`` is the user's existing ``novel:`` mapping as a
    raw YAML string.  When ``None``, a sane default block is used.
    """

    if template_name not in _templates.PIPELINE_TEMPLATES:
        raise ConfigError(
            f"unknown template {template_name!r}; "
            f"expected one of: {sorted(_templates.PIPELINE_TEMPLATES)}"
        )
    novel_raw = novel_block or _DEFAULT_NOVEL_BLOCK
    novel = _yaml.safe_load(novel_raw)
    if not isinstance(novel, Mapping):
        raise ConfigError("novel: section must be a mapping")

    stage_ids = list(_templates.PIPELINE_TEMPLATES[template_name])
    prompts: dict[str, str] = {}
    stages_payload: list[dict[str, Any]] = []
    for sid in stage_ids:
        tpl = _templates.get_template(sid)
        stages_payload.append(tpl.to_dict())
        prompts[tpl.prompt_file] = tpl.prompt_text

    payload: dict[str, Any] = {
        "novel": dict(novel),
        "pipeline": {
            "scaffold_from": template_name,
            "stages": stages_payload,
        },
        "execution": {
            "batch_size": {"outline": 50, "chapter": 3},
            "max_review_iterations": 3,
            "review_model": "claude-sonnet-4-6",
            "write_model": "claude-opus-4-7",
        },
    }
    return payload, prompts


@app.command()
def init(
    template: str = typer.Option(
        "long-epic",
        "--template",
        "-t",
        help="Template name (long-epic, short-story, series).",
    ),
    project_dir: Path = typer.Option(
        Path("."),
        "--dir",
        "-d",
        help="Project directory (created if missing).",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite existing files.",
    ),
) -> None:
    """Scaffold a fresh v4 project (yaml + prompts/).

    Per spec §5.5: this command **does not** generate ``outline/`` or
    other user-seed files; the user supplies them.
    """

    project_dir = project_dir.expanduser().resolve()
    if project_dir.exists() and not project_dir.is_dir():
        err_console.print(f"[red]Not a directory:[/red] {project_dir}")
        raise typer.Exit(code=2)
    try:
        payload, prompts = _render_v4_yaml(template_name=template)
    except ConfigError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    _ensure_dir(project_dir)
    yaml_path = project_dir / "novel-project.yaml"
    prompts_dir = project_dir / "prompts"
    if yaml_path.exists() and not force:
        err_console.print(
            f"[red]Refusing to overwrite:[/red] {yaml_path} (use --force to override)"
        )
        raise typer.Exit(code=2)
    _atomic_write(yaml_path, _yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))
    _ensure_dir(prompts_dir)
    for fname, body in prompts.items():
        target = prompts_dir / fname
        if target.exists() and not force:
            continue
        _atomic_write(target, body)
    console.print(
        f"[green]Scaffolded:[/green] {yaml_path} + {len(prompts)} prompt file(s)"
    )
    console.print(
        "  Next: prepare outline/ + CLAUDE.md, then `novelforge run`."
    )


@app.command()
def migrate(
    config: Path = typer.Option(
        ...,
        "--config",
        "-c",
        help="Path to a v3 novel-project.yaml to migrate.",
    ),
    out: Optional[Path] = typer.Option(
        None,
        "--out",
        help="Write the migrated yaml to this path (default: dry-run to stdout).",
    ),
    write: bool = typer.Option(
        False,
        "--write",
        help="Overwrite the source yaml in place (backs up to <name>.bak).",
    ),
) -> None:
    """Migrate a v3 yaml to v4 form.

    Output policy:

    - ``--out new.yaml`` → write the migrated yaml to ``new.yaml``
      (default behaviour, dry-run safe).
    - ``--write`` → overwrite the source yaml; a ``.bak`` backup is
      written next to it.
    - ``--out`` and ``--write`` are mutually exclusive.
    """

    if out is not None and write:
        err_console.print(
            "[red]Refusing: --out and --write are mutually exclusive.[/red]"
        )
        raise typer.Exit(code=2)
    src = _resolve_path(config)
    if not src.exists():
        err_console.print(f"[red]Config file not found:[/red] {src}")
        raise typer.Exit(code=2)
    try:
        raw = _yaml.safe_load(src.read_text(encoding="utf-8"))
    except _yaml.YAMLError as exc:
        err_console.print(f"[red]Source yaml is invalid:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    if not isinstance(raw, Mapping):
        err_console.print("[red]Source yaml root must be a mapping.[/red]")
        raise typer.Exit(code=2)
    pipeline = raw.get("pipeline")
    if not isinstance(pipeline, Mapping):
        err_console.print(
            "[red]Source yaml has no pipeline: section; nothing to migrate.[/red]"
        )
        raise typer.Exit(code=2)
    template = pipeline.get("template", "long-epic")
    if template not in _templates.PIPELINE_TEMPLATES:
        err_console.print(
            f"[red]Unknown template {template!r}; cannot migrate.[/red]"
        )
        raise typer.Exit(code=2)
    # Preserve the user's novel: and execution: blocks as-is.
    novel_block_raw = src.read_text(encoding="utf-8")
    # Crude extraction: the indented body of the ``novel:`` block.
    # We strip the leading ``novel:`` line and dedent the body so the
    # YAML loader sees a bare mapping (not another ``novel:`` wrapper
    # or an indented block that conflicts with the outer document).
    novel_lines: list[str] = []
    in_novel = False
    indent: Optional[int] = None
    for line in novel_block_raw.splitlines():
        if line.startswith("novel:"):
            in_novel = True
            continue  # drop the "novel:" key itself
        elif in_novel and line and not line.startswith(" ") and line.endswith(":"):
            break
        if in_novel:
            if not line.strip():
                novel_lines.append("")
                continue
            if indent is None:
                # First non-blank line sets the indent.
                indent = len(line) - len(line.lstrip(" "))
            # Dedent relative to the first line.
            stripped = line[indent:] if len(line) >= indent else line
            novel_lines.append(stripped)
    novel_block = "\n".join(novel_lines).strip() or None
    payload, prompts = _render_v4_yaml(
        template_name=str(template),
        novel_block=novel_block,
    )
    if "execution" in raw and isinstance(raw["execution"], Mapping):
        payload["execution"] = dict(raw["execution"])
    rendered = _yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)

    if write:
        backup = src.with_suffix(src.suffix + ".bak")
        if not backup.exists():
            _atomic_write(backup, src.read_text(encoding="utf-8"))
        _atomic_write(src, rendered)
        prompts_dir = src.parent / "prompts"
        _ensure_dir(prompts_dir)
        for fname, body in prompts.items():
            target = prompts_dir / fname
            _atomic_write(target, body)
        console.print(
            f"[green]Migrated in place:[/green] {src} (backup at {backup}) + "
            f"{len(prompts)} prompt file(s)"
        )
        return
    if out is not None:
        target = _resolve_path(out)
        _ensure_dir(target.parent)
        _atomic_write(target, rendered)
        prompts_dir = target.parent / "prompts"
        _ensure_dir(prompts_dir)
        for fname, body in prompts.items():
            _atomic_write(prompts_dir / fname, body)
        console.print(
            f"[green]Migrated:[/green] wrote {target} + {len(prompts)} prompt file(s)"
        )
        return
    # Dry-run: dump to stdout for inspection.
    console.print(rendered)


if __name__ == "__main__":  # pragma: no cover
    main()
