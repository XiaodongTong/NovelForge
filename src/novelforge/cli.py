"""Command-line entry point for the NovelForge engine.

Implemented with Typer.  Five subcommands are registered:

- ``novelforge run``     — start a fresh end-to-end pipeline run
- ``novelforge resume``  — continue from the last valid checkpoint
- ``novelforge status``  — print current state, progress, and token usage
- ``novelforge validate``— validate a ``novel-project.yaml`` configuration
- ``novelforge init``    — scaffold a fresh project from a built-in template

The CLI is intentionally thin: argument parsing and exit codes live here,
business logic lives in the ``config``/``state``/``orchestrator`` modules.

v4 notes:

- The v3 ``migrate`` command has been removed; users edit their yaml
  by hand or re-run ``init`` (spec §AC-15).
- The v3 fields ``pipeline.template`` / ``pipeline.stages_override`` /
  ``pipeline.scaffold_from`` are rejected at load time.
- v4.1 (PR-1): ``init`` now scaffolds user seed files
  (``outline/premise.md`` + ``outline/world.md`` + ``CLAUDE.md``)
  and the runtime directories with ``.gitkeep`` markers.  Use
  ``--skeleton-only`` to opt out of the user seeds.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping, Optional

import typer
import yaml as _yaml
from rich.console import Console

from . import __version__
from . import templates as _templates
from .config import ConfigError, NovelProjectConfig, load_config, stage_ids_for
from .orchestrator import Orchestrator
from .state import StateStore
from .utils.fs import atomic_write as _atomic_write, ensure_dir as _ensure_dir
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

    console.print(
        f"[green]Config OK:[/green] {cfg.novel.target_chapters} chapter(s)"
    )
    console.print(f"  novel      : {cfg.novel.title!r} ({cfg.novel.genre})")
    console.print(f"  stages     : {' -> '.join(stage_ids_for(cfg))}")
    if cfg.novel.words_per_chapter:
        console.print(
            f"  word budget: {cfg.novel.words_per_chapter[0]}-"
            f"{cfg.novel.words_per_chapter[1]} chars/chapter"
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
    extra = snapshot.get("extra", {}) or {}

    target = cfg.novel.target_chapters
    payload = {
        "current_stage": snapshot.get("current_stage"),
        "target_chapters": target,
        "progress": {
            "chapters_written": int(progress.get("chapters_written", 0) or 0),
            "chapters_reviewed": int(progress.get("chapters_reviewed", 0) or 0),
            "total_words": int(progress.get("total_words", 0) or 0),
        },
        "token_usage": {
            "total_input": int(token_usage.get("total_input", 0) or 0),
            "total_output": int(token_usage.get("total_output", 0) or 0),
        },
        "last_checkpoint_at": snapshot.get("last_checkpoint_at"),
        "paused": bool(snapshot.get("paused", False)),
        "paused_reason": snapshot.get("paused_reason"),
        "stage_attempts": extra.get("stage_attempts", {}),
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
# init command (scaffolds a fresh project from a built-in template)
# --------------------------------------------------------------------------- #


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


# Runtime directories that ``init`` always materialises (with a
# ``.gitkeep`` so they survive an empty-tree commit).  The directory
# tree is documented in ``templates.CLAUDE_TEMPLATE``; keeping both
# lists here in lock-step is the spec §AC-1 contract.
RUNTIME_DIRS: tuple[str, ...] = (
    "characters",
    "chapters-outline",
    "output/summaries",
    "output/meta",
    "output/chapters",
    "output/review",
)


# User-facing seed file templates.  Short and in Chinese to match the
# target audience (spec §6 OQ-1).  Both include visible section
# headings so ``_check_seed_files`` (which only checks for existence)
# sees them as "non-empty" without committing to content validation.
PREMISE_TEMPLATE = """\
# Premise

## 故事前提（核心冲突）

用 1-2 句话回答：**主角要在哪个不可调和的矛盾里行动**。

## 主角北极星

主角在故事结尾想达成（或被迫接受）的状态是什么？不要写愿望，要写**代价**。

## 世界底线

这个故事的"物理 / 道德底线"是什么？什么**绝对不能**发生？（例：死人不会复活 / 主角不会主动杀人）

## 调性与篇幅

- 调性：例如 melancholic post-apocalypse / 热血群像 / 慢热悬疑
- 篇幅：target_chapters × words_per_chapter 的预估总字数

> 删掉这些占位行，开始写你的故事前提。
"""


WORLD_TEMPLATE = """\
# World

## 势力 / 阵营

列出主要阵营，每个一行：

- <阵营名>：一句话定位 + 与主角的初始关系

## 时代 / 技术基线

一句话交代故事发生的时间坐标（古代 / 近代 / 未来）+ 可用 / 不可用的技术。

## 地理边界

故事**会**发生的区域（不要列"世界地图"，要列**会**被用到的三五个关键地点）。

## 调性关键词

5 个以内，每个一行；这些词会被引擎用作风格锚点。

> 删掉这些占位行，开始写你的世界设定。
"""


def _render_template_yaml(
    *,
    template_name: str,
    novel_block: Optional[str] = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Return a v4 yaml payload + the prompts to materialise.

    ``novel_block`` is the user's existing ``novel:`` mapping as a
    raw YAML string.  When ``None``, a sensible default block is used.
    """

    template = _templates.get_template(template_name)
    novel_raw = novel_block or _DEFAULT_NOVEL_BLOCK
    novel = _yaml.safe_load(novel_raw)
    if not isinstance(novel, Mapping):
        raise ConfigError("novel: section must be a mapping")

    prompts: dict[str, str] = {}
    for stage in template.stages:
        # filename matches the ``prompt`` field set in the template.
        prompt_file = stage.prompt.split("/")[-1]
        prompts[prompt_file] = template.prompts[stage.id]

    payload: dict[str, Any] = {
        "novel": dict(novel),
        "pipeline": {
            "stages": template.to_payload(),
        },
        "execution": {
            "batch_size": {"outline": 50, "chapter": 3},
            "max_review_iterations": 3,
            "review_model": "claude-sonnet-4-6",
            "write_model": "claude-opus-4-7",
        },
    }
    return payload, prompts


# --------------------------------------------------------------------------- #
# init helpers
# --------------------------------------------------------------------------- #


def _write_seed(path: Path, body: str, *, force: bool) -> bool:
    """Write a seed file to ``path``; skip if it already exists.

    Returns ``True`` when the file was actually written (either
    because it didn't exist or because ``force`` was set).  Returns
    ``False`` when the file already existed and ``force`` was False
    (i.e. the user's content was preserved, spec §AC-5).
    """

    if path.exists() and not force:
        return False
    if path.exists() and force:
        _maybe_warn_overwrite(path)
    _ensure_dir(path.parent)
    _atomic_write(path, body)
    return True


def _ensure_gitkeep(dir_path: Path, *, force: bool) -> bool:
    """Make sure ``dir_path`` exists and has a ``.gitkeep`` marker.

    ``dir_path`` is interpreted relative to the user's project root.
    Returns ``True`` when something was actually written or the
    directory was newly created.
    """

    created = False
    if not dir_path.exists():
        _ensure_dir(dir_path)
        created = True
    keep = dir_path / ".gitkeep"
    if keep.exists() and not force:
        return created
    if keep.exists() and force:
        _maybe_warn_overwrite(keep)
    _atomic_write(keep, "")
    return True


def _maybe_warn_overwrite(path: Path) -> None:
    """Print a console warning when ``init --force`` overwrites a file."""

    err_console.print(f"[yellow]WARN:[/yellow] overwriting {path}")


@app.command()
def init(
    template: str = typer.Option(
        "long-epic",
        "--template",
        "-t",
        help="Template name (long-epic, short-story).",
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
    skeleton_only: bool = typer.Option(
        False,
        "--skeleton-only",
        help=(
            "Skip writing user seed files (outline/, CLAUDE.md); only "
            "materialise yaml + prompts/ + empty runtime dirs.  Useful "
            "for CI / re-templating an existing project."
        ),
    ),
) -> None:
    """Scaffold a fresh v4 project (yaml + prompts + seeds + dirs).

    The default run writes:

    - ``novel-project.yaml`` + every ``prompts/*.md`` (template-driven)
    - ``outline/premise.md`` + ``outline/world.md`` + ``CLAUDE.md``
      (user-facing seeds with sectioned placeholders, spec §AC-3)
    - Six runtime directories with ``.gitkeep`` markers
      (spec §AC-1)

    With ``--skeleton-only`` the user seeds are **not** written; only
    the engine-side scaffold remains (yaml + prompts + empty dirs).
    Combine with ``--force`` to refresh an existing scaffold without
    touching user content (spec §AC-15).
    """

    project_dir = project_dir.expanduser().resolve()
    if project_dir.exists() and not project_dir.is_dir():
        err_console.print(f"[red]Not a directory:[/red] {project_dir}")
        raise typer.Exit(code=2)
    try:
        payload, prompts = _render_template_yaml(template_name=template)
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
    if yaml_path.exists() and force:
        _maybe_warn_overwrite(yaml_path)
    _atomic_write(yaml_path, _yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))
    _ensure_dir(prompts_dir)
    for fname, body in prompts.items():
        target = prompts_dir / fname
        if target.exists() and not force:
            continue
        if target.exists() and force:
            _maybe_warn_overwrite(target)
        _atomic_write(target, body)

    # --- 1. User seed files (skipped under --skeleton-only) ----------
    seeds_written = 0
    if not skeleton_only:
        seeds: tuple[tuple[Path, str], ...] = (
            (project_dir / "outline" / "premise.md", PREMISE_TEMPLATE),
            (project_dir / "outline" / "world.md", WORLD_TEMPLATE),
            (project_dir / "CLAUDE.md", _templates.CLAUDE_TEMPLATE),
        )
        for target, body in seeds:
            if _write_seed(target, body, force=force):
                seeds_written += 1

    # --- 2. Runtime directories with .gitkeep -------------------------
    for rel in RUNTIME_DIRS:
        _ensure_gitkeep(project_dir / rel, force=force)

    # --- 3. Next-steps hint ------------------------------------------
    if skeleton_only:
        next_hint = (
            "no user seeds written (--skeleton-only).  "
            "Add outline/premise.md, outline/world.md, CLAUDE.md yourself "
            "before running `novelforge run`."
        )
    else:
        next_hint = (
            "Next: edit `outline/premise.md` + `outline/world.md`, "
            "see `CLAUDE.md` for the writing rules.  "
            "Then `novelforge validate --config novel-project.yaml` "
            "and `novelforge run --use-mock`."
        )

    console.print(
        f"[green]Scaffolded:[/green] {yaml_path} + {len(prompts)} prompt file(s)"
        f" + {seeds_written} seed file(s)"
        f" + {len(RUNTIME_DIRS)} runtime dir(s)"
    )
    console.print(f"  {next_hint}")


if __name__ == "__main__":  # pragma: no cover
    main()
