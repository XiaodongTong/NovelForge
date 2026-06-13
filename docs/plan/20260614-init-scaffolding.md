# `novelforge init` 脚手架增强方案

> 日期：2026-06-14
> 状态：草案 / 待评审
> 关联：`docs/plan/novelforge-design.md` §5.5、`docs/plan/pipeline-customization.md`

## 1. 背景与问题

当前 `init` 命令（`src/novelforge/cli.py:404-459`）的行为过于"瘦"：

- 只产出两个东西：`novel-project.yaml` 和 `prompts/*.md`
- 源码注释明确写："**does not generate** `outline/` or other user-seed files; the user supplies them."
- 用户跑完 `init` 之后还必须自己手动 `mkdir outline`、`touch outline/premise.md`、`touch outline/world.md`、想好角色档案放哪里……否则 `validate` 直接报种子缺失，`run` 无法启动。

这违背"脚手架应让用户拿到可立刻填空的项目骨架"的工程直觉。`samples/minimal-novel/` 已经有完整目录结构（`outline/`、`prompts/`、`CLAUDE.md`），但 `init` 没有复制这套结构。

### 用户明确诉求

1. **故事背景前提** —— 落到 `outline/premise.md`（用户填）
2. **世界设定** —— 落到 `outline/world.md`（用户填）
3. **角色档案目录** —— 引擎后续要把角色档案写到该路径（当前 templates 写到单文件 `output/meta/characters.md`）
4. **目录大纲的目录和文件** —— 引擎后续要把章节大纲写到该位置（当前 templates 写到 `output/summaries/plot.md`）

诉求 3、4 隐含着一个路径变化：从"散落在 `output/` 下的单文件"改为"有专属目录的多文件结构"。

## 2. 目标

`init` 跑完之后，用户应该得到：

- ✅ 一个能直接通过 `novelforge validate` 的最小项目骨架（用户填好种子后即可 `run`）
- ✅ 所有需要用户手填的文件以**模板**形式存在（带结构占位 + 写作提示，而非完全空白）
- ✅ 所有引擎运行时要写入的目录提前创建好（带 `.gitkeep`），引擎写入时不必 `mkdir -p`
- ✅ 路径布局直观，新人看一眼目录树就能猜到每个文件的角色

**非目标**：

- 不在本次方案里改 `prompts/*.md` 的内容（保持现状）
- 不动 `migrate` 命令的逻辑（v3 → v4 迁移单独考虑）
- 不引入"交互式问答填空"（先做静态模板，交互式后续可加）

## 3. 当前现状摘要

### 3.1 `init` 当前输出

```
<project_dir>/
├── novel-project.yaml        # 完整 v4 yaml，包含 stages、execution、novel 块
└── prompts/
    ├── generate-outline.md
    ├── review-outline.md
    ├── design-characters.md
    ├── review-characters.md
    ├── simulate-plot.md
    ├── review-simulation.md
    ├── write-chapter.md
    ├── review-chapter.md
    ├── full-consistency-check.md
    └── final-polish.md
```

### 3.2 `samples/minimal-novel/` 的实际结构（参考标杆）

```
samples/minimal-novel/
├── CLAUDE.md                 # 写作约束（constraint）
├── novel-project.yaml
├── outline/
│   ├── premise.md            # 故事前提（seed）
│   └── world.md              # 世界设定（seed）
└── prompts/
    └── *.md
```

### 3.3 templates.py 的 output 路径（决定引擎往哪里写）

| 阶段 | 当前 output 路径 | 写入形态 |
|------|-----------------|---------|
| `generate_outline` | `output/summaries/plot.md` | 单文件 |
| `design_characters` | `output/meta/characters.md` | 单文件 |
| `simulate_plot` | `output/summaries/plot-simulation.md` | 单文件 |
| `write_chapter` | `output/chapters/{{num:03d}}-{{title\|slug}}.md` | 多文件（split） |
| `review_*` | `output/review/*.json` | 单文件 |
| `full_consistency_check` | `output/review/consistency-report.md` | 单文件 |
| `final_polish` | `output/review/final-polish-notes.md` | 单文件 |

## 4. 设计方案

给出三个候选方案，**推荐方案 B**。

### 方案 A：保守增量（最小改动）

只在 `init` 里加种子文件，**不改 templates.py**：

```
<project_dir>/
├── novel-project.yaml
├── CLAUDE.md                          # 新增（最小模板）
├── outline/                           # 新增
│   ├── premise.md                     # 用户填空模板
│   └── world.md                       # 用户填空模板
├── output/                            # 新增（占位目录）
│   ├── summaries/.gitkeep
│   ├── meta/.gitkeep
│   ├── chapters/.gitkeep
│   └── review/.gitkeep
└── prompts/
    └── *.md（现状）
```

- ✅ 改动最小，只动 `cli.py` 的 `init` 函数
- ❌ 没满足用户诉求 3、4（角色档案 / 章节大纲仍是单文件，散在 `output/`）
- ❌ `output/` 是运行时产物，预创建略显多余（引擎自己会 `mkdir`）

### 方案 B：折中重构（推荐）✅

`init` 生成种子模板 + 重定位"长产物"到项目根的语义目录：

```
<project_dir>/
├── novel-project.yaml
├── CLAUDE.md                          # 写作约束模板
├── outline/                           # 用户种子区
│   ├── premise.md                     # ← 用户填：故事前提
│   └── world.md                       # ← 用户填：世界设定
├── characters/                        # ← 引擎填：角色档案（design_characters）
│   └── .gitkeep
├── chapters-outline/                  # ← 引擎填：章节大纲（generate_outline）
│   └── .gitkeep
├── output/                            # 运行时产物区（不变）
│   ├── summaries/.gitkeep
│   ├── meta/.gitkeep
│   ├── chapters/.gitkeep
│   └── review/.gitkeep
└── prompts/
    └── *.md
```

同时修改 `templates.py`：

| 阶段 | 旧 output | 新 output |
|------|----------|----------|
| `generate_outline` | `output/summaries/plot.md` | `chapters-outline/outline.md`（仍是单文件，但路径语义化） |
| `design_characters` | `output/meta/characters.md` | `characters/{{slug\|default("main")}}.md`（多文件，split） |

`design_characters` 改成多文件需要新加 `split` 字段（按 `# <Character Name>` 切分）。

- ✅ 满足用户全部 4 条诉求
- ✅ 目录语义清晰：`outline/`（输入）→ `characters/` + `chapters-outline/`（中间产物）→ `output/chapters/`（最终章节）
- ✅ 角色档案拆分后，引擎可以单独 review 某个角色，迁移工具也能定向替换
- ⚠️ 改了 templates.py 默认 output，需要：
  - 同步更新 sample 项目的 yaml（如果有硬编码）
  - `migrate` 命令对 v3 → v4 旧 output 路径的兼容（保留 fallback）
  - 新增 split 正则、orchestrator 的写入逻辑适配

### 方案 C：激进重构（不推荐）

把所有阶段产物都搬出 `output/`，按"输入/中间/最终"三层重组：

```
<project_dir>/
├── 01-seeds/      # outline/premise.md, outline/world.md, CLAUDE.md
├── 02-blueprint/  # characters/, chapters-outline/, plot-simulation.md
├── 03-chapters/   # 最终章节正文
├── 04-review/     # 所有 review JSON + 报告
└── prompts/
```

- ✅ 目录语义最强
- ❌ 改动面巨大（每个 StageTemplate.output、orchestrator 写入路径、review 路径、tests 全部要动）
- ❌ 用户已习惯 `output/` 子目录，重命名带来迁移成本
- ❌ 收益与成本不匹配

## 5. 推荐方案 B 的详细规格

### 5.1 `init` 输出清单

| 路径 | 形态 | 内容策略 | `--force` 行为 |
|------|------|---------|---------------|
| `novel-project.yaml` | 文件 | 完整 v4 yaml（现状） | 不存在或 `--force` 才写；存在则报错退出 |
| `CLAUDE.md` | 文件 | **自包含模板**：写作规则 + 目录大纲 + 每个文件的要求（用户/引擎读写边界） | **存在则 skip**（用户已经写过不想被覆盖） |
| `outline/premise.md` | 文件 | 模板（带结构占位） | 存在则 skip |
| `outline/world.md` | 文件 | 模板（带结构占位） | 存在则 skip |
| `characters/.gitkeep` | 空文件 | 空占位 | 存在则 skip |
| `chapters-outline/.gitkeep` | 空文件 | 空占位 | 存在则 skip |
| `output/summaries/.gitkeep` | 空文件 | 空占位 | 存在则 skip |
| `output/meta/.gitkeep` | 空文件 | 空占位 | 存在则 skip |
| `output/chapters/.gitkeep` | 空文件 | 空占位 | 存在则 skip |
| `output/review/.gitkeep` | 空文件 | 空占位 | 存在则 skip |
| `prompts/*.md` | 文件（10 个） | 现状 | 存在则 skip（现状） |

**新加 CLI flag**：

- `--skeleton-only`：跳过 `outline/`、`CLAUDE.md` 种子模板，只生成 yaml + prompts + 空目录（适合脚本化场景）
- 默认行为：全套生成

### 5.2 种子文件模板内容

#### `outline/premise.md`

```markdown
# Premise

<!-- 用 1-3 句话写出故事的核心冲突与主角的根本目标。
     这一段是引擎生成大纲、角色、章节时的最高优先级约束。 -->

## Core conflict

<!-- 例如：个人成长 vs 守护所爱；自由 vs 秩序；复仇 vs 救赎 -->

## Protagonist's north star

<!-- 主角最想达成的一件事，所有章节都该服务于它 -->
```

#### `outline/world.md`

```markdown
# World

<!-- 描述故事发生的世界：时代背景、地理、社会结构、关键规则。
     不需要面面俱到，只写引擎需要遵守的硬性设定。 -->

## Factions

<!-- 列出主要势力，每个一句话定位 -->

## Tone

<!-- 1-2 个形容词，例如：melancholic wuxia / fast-paced cyberpunk -->
```

#### `CLAUDE.md`（自包含：写作规则 + 目录大纲）

CLAUDE.md 同时承担两个角色：(1) 引擎写作约束，(2) 项目目录与文件要求的索引。引擎运行时会把它作为全局 system context 读入，因此把目录结构写进来，让模型清楚每个文件的作用与读写边界。

模板内容如下（`init` 直接落盘）：

```markdown
# NovelForge 项目约束

> 本文件既是引擎（Claude）的写作约束，也是项目目录与文件要求的索引。
> 引擎运行时会读取本文件作为全局上下文，请保持内容准确，删除所有
> `<!-- ... -->` 注释后再 `novelforge run`。

## 目录大纲

<project_dir>/
├── novel-project.yaml        # 项目配置（pipeline.stages / execution）
├── CLAUDE.md                 # 本文件：写作规则 + 目录索引
├── outline/                  # 【用户填写】故事种子区
│   ├── premise.md            # ← 故事前提：核心冲突、主角北极星
│   └── world.md              # ← 世界设定：势力、时代、调性
├── characters/               # 【引擎生成】角色档案区（design_characters）
│   └── <slug>.md             #    每个角色一个 md 文件
├── chapters-outline/         # 【引擎生成】章节大纲区（generate_outline）
│   └── outline.md            #    章节级 beats
├── output/                   # 【引擎生成】运行时产物
│   ├── summaries/            #    plot 模拟、摘要
│   ├── meta/                 #    元数据
│   ├── chapters/             #    最终章节正文（write_chapter）
│   └── review/               #    所有 review JSON + 报告
└── prompts/                  # 各阶段的 prompt 模板
    └── *.md

## 文件要求

### `outline/premise.md`（必填 · 用户写）

用不超过 3 句话写清楚三件事：

1. **核心冲突**：主角的根本张力（如：成长 vs 守护 / 自由 vs 秩序）。
2. **主角北极星**：主角最想达成的一件事。
3. **不可妥协的设定**：引擎写作时绝对不能违反的硬约束。

> 引擎用途：作为 `generate_outline` / `write_chapter` 的最高优先级上下文。

### `outline/world.md`（必填 · 用户写）

至少写清楚：

1. **势力**：列出主要势力，每个一句话定位。
2. **时代 / 地理**：故事发生的时空背景。
3. **调性**：1-2 个形容词（如：melancholic wuxia）。

> 引擎用途：作为 `design_characters` / `simulate_plot` 的世界约束。

### `characters/<slug>.md`（引擎生成 · 不要手填）

由 `design_characters` 阶段写入，**每个角色一个文件**。

- 文件名规则：`<角色名拼音或英文 slug>.md`，例如 `li-ming.md`
- 内容结构：

  ```
  # <角色名>

  **Role**: ...
  **Voice**: ...
  **Relationships**: ...
  **Arc**: ...
  ```

> 引擎用途：被 `review_characters` / `simulate_plot` / `write_chapter` 读作上下文。
> 用户可以手改（润色、增删字段），但不要删除整个文件。

### `chapters-outline/outline.md`（引擎生成 · 不要手填）

由 `generate_outline` 阶段写入。结构：

```
## Chapter N - <标题>

<1-2 句关键 beat 描述>
```

> 引擎用途：被 `write_chapter` 切分消费（按 `## Chapter N` 标题逐章写）。
> 用户可以在生成后手动调整章节顺序、删除某章，但不要改标题格式。

### `output/`（引擎生成）

运行时产物，用户一般无需关注。包含：

- `output/summaries/`：plot 模拟、摘要
- `output/meta/`：附加元数据
- `output/chapters/`：最终章节正文（`NNN-slug.md` 命名）
- `output/review/`：所有 review JSON 与一致性报告

删除后引擎会按需重建。

### `prompts/*.md`（可改 · 用户写）

每个阶段的 prompt 模板，对应 `novel-project.yaml` 里 `pipeline.stages[].prompt` 的相对路径。可以自由改写 prompt 文本，但不要改文件名（yaml 引用的是文件名）。

## 写作规则

<!-- 这些是引擎写作时必须遵守的规则。删除或改写以下条目以适配你的小说。 -->

1. 章节字数控制在 `min_words` ~ `max_words` 之间，用细节而非废话填充。
2. 保持视角一致（如无特殊说明，使用第三人称限知）。
3. 每章结尾留一个小反转（疑问、背叛、代价）以推动阅读。
4. 避免现代俚语、品牌名、直接的网络梗。
```

### 5.3 `templates.py` 修改

```python
# 1. generate_outline：路径语义化
StageTemplate(
    id="generate_outline",
    model="claude-opus-4-7",
    output="chapters-outline/outline.md",  # 旧: output/summaries/plot.md
    prompt_text=...,  # 不变
)

# 2. design_characters：改成多文件 + split
StageTemplate(
    id="design_characters",
    model="claude-opus-4-7",
    output="characters/{{slug|default(\"main\")}}.md",  # 旧: output/meta/characters.md
    split=r"^#\s+(?P<slug>.+?)\s*$",  # 按一级标题切分
    prompt_text=...,  # 现有 prompt 已要求 "# <Character Name>" 开头，兼容
)
```

### 5.4 模板常量归属

`PREMISE_TEMPLATE` / `WORLD_TEMPLATE` 较短，可以放在 `cli.py` 顶部常量区。

`CLAUDE_TEMPLATE` 内容较长（目录树 + 5 个文件要求 + 写作规则，约 80 行 markdown），建议放到 `src/novelforge/templates.py` 末尾，作为 `CLAUDE_TEMPLATE: str` 常量导出，避免 `cli.py` 膨胀。后续如果要支持中英双语或多套写作规则（如玄幻 vs 科幻），也方便扩展为 `dict[str, str]`。

### 5.5 `cli.py` 的 `init` 改动草图

```python
@app.command()
def init(
    template: str = typer.Option("long-epic", "--template", "-t", ...),
    project_dir: Path = typer.Option(Path("."), "--dir", "-d", ...),
    force: bool = typer.Option(False, "--force", ...),
    skeleton_only: bool = typer.Option(
        False, "--skeleton-only",
        help="Skip seed templates (outline/, CLAUDE.md); only scaffold yaml + prompts + empty dirs.",
    ),
) -> None:
    """Scaffold a fresh v4 project (yaml + prompts/ + seeds/ + run dirs)."""
    # ... 现有的路径检查、yaml 渲染 ...

    # 现有：写 yaml + prompts
    _atomic_write(yaml_path, ...)
    _ensure_dir(prompts_dir)
    for fname, body in prompts.items():
        ...

    # 新增：种子模板（除非 --skeleton-only）
    if not skeleton_only:
        _write_seed(project_dir / "outline/premise.md", PREMISE_TEMPLATE, force=force)
        _write_seed(project_dir / "outline/world.md", WORLD_TEMPLATE, force=force)
        _write_seed(project_dir / "CLAUDE.md", _templates.CLAUDE_TEMPLATE, force=force)

    # 新增：运行时目录占位
    for run_dir in (
        "characters", "chapters-outline",
        "output/summaries", "output/meta",
        "output/chapters", "output/review",
    ):
        _ensure_gitkeep(project_dir / run_dir, force=force)

    # 新增：打印 next-steps，引导用户填哪些文件
    console.print("[green]Scaffolded:[/green] ...")
    console.print("  Next: edit outline/premise.md + outline/world.md, then `novelforge validate`.")
    console.print("  See CLAUDE.md → 目录大纲 → 文件要求 for what to fill.")
```

### 5.5 `_write_seed` / `_ensure_gitkeep` 行为

```python
def _write_seed(path: Path, body: str, *, force: bool) -> bool:
    """Write seed file unless it exists (respect --force)."""
    if path.exists() and not force:
        return False
    _ensure_dir(path.parent)
    _atomic_write(path, body)
    return True

def _ensure_gitkeep(dir_path: Path, *, force: bool) -> bool:
    """Create dir + .gitkeep if missing."""
    _ensure_dir(dir_path)
    keep = dir_path / ".gitkeep"
    if keep.exists() and not force:
        return False
    _atomic_write(keep, "")
    return True
```

## 6. 影响面分析

### 6.1 需要修改的文件

| 文件 | 改动类型 | 改动量 |
|------|---------|-------|
| `src/novelforge/cli.py` | `init` 函数扩展、新加 helper、`PREMISE_TEMPLATE` / `WORLD_TEMPLATE` 常量 | ~80 行 |
| `src/novelforge/templates.py` | (1) 改 2 个 StageTemplate 的 output（方案 B）；(2) 新增 `CLAUDE_TEMPLATE` 常量 | ~100 行 |
| `samples/minimal-novel/novel-project.yaml` | 同步新 output 路径 | 几行 |
| `tests/test_cli.py`（或同名） | 新增 init 脚手架断言 | ~50 行 |
| `tests/test_templates.py` | 新增 split 正则测试 | ~20 行 |
| `src/novelforge/migrate.py`（若有 v3→v4 fallback） | 旧路径兼容 | 视情况 |

### 6.2 向后兼容

- **新 init 出来的项目**：使用新路径
- **已存在的 v4 项目**：用户的 yaml 里 stages.output 是旧路径，引擎照旧——`init` 不动他们
- **migrate 兼容**：v3 yaml 迁移到 v4 时，默认走新路径；如果用户原 yaml 已显式写了 `output:`，保留不动

### 6.3 风险

1. **`design_characters` 改成 split 后**，下游 `review_characters`、`simulate_plot` 读取它的逻辑需要适配（从读单文件变成读目录）。需要确认 orchestrator 的 stage input 装配逻辑。
2. **`output/` 与 `characters/`、`chapters-outline/` 并存**会让用户混淆——需要在 `CLAUDE.md` 或 README 里说明每个目录的语义。
3. **`--force` 全局覆盖**会让种子文件被空模板覆盖，需要让用户明确意识到这一点（CLI 输出 warning）。

## 7. 测试计划

新增/扩展的测试用例：

### 7.1 `tests/test_cli_init.py`（新增）

- `test_init_creates_yaml_and_prompts`（已有，保留）
- `test_init_creates_seed_files`：断言 `outline/premise.md`、`outline/world.md`、`CLAUDE.md` 存在且非空
- `test_init_creates_runtime_dirs`：断言 6 个目标目录 + `.gitkeep` 存在
- `test_init_seed_skip_if_exists`：预先放一个非空 `outline/premise.md`，跑 init，断言内容**未被覆盖**
- `test_init_seed_force_overwrites`：同上但带 `--force`，断言被覆盖
- `test_init_skeleton_only_skips_seeds`：`--skeleton-only` 跑后，断言 `outline/`、`CLAUDE.md` 不存在
- `test_init_idempotent`：连续跑两次，第二次应该无错误无覆盖

### 7.2 `tests/test_templates.py`（扩展）

- `test_design_characters_has_split`：断言 `split` 字段非空
- `test_design_characters_split_regex_matches`：给定示例角色档案，断言按 `# <Name>` 切分正确
- `test_generate_outline_output_path`：断言新路径

### 7.3 `tests/test_orchestrator.py`（扩展，确认 split 适配）

- `test_design_characters_writes_multiple_files`：跑 mock design_characters，断言 `characters/` 下有多个 `.md` 文件而非单文件

### 7.4 手动验证

```bash
# 在临时目录跑全套
cd /tmp && rm -rf demo && mkdir demo && cd demo
novelforge init -t long-epic
novelforge validate           # 应该退出 0（或仅 1 个 seed 未填的 warning）
# 填好 premise.md / world.md
novelforge run --use-mock     # 用 mock adapter 跑通整个 pipeline
ls characters/                # 应该看到 mock 生成的角色档案
ls chapters-outline/          # 应该看到 outline.md
```

## 8. 实施步骤建议

按以下顺序提交，每步独立可测：

1. **PR-1：种子模板**（不涉及 templates.py 的 StageTemplate）
   - 在 `templates.py` 新增 `CLAUDE_TEMPLATE` 常量（目录大纲 + 文件要求 + 写作规则）
   - 在 `cli.py` 加 `PREMISE_TEMPLATE` / `WORLD_TEMPLATE` 常量
   - `init` 写这 3 个文件 + 6 个 `.gitkeep`
   - 加 `--skeleton-only` flag
   - 配套测试

2. **PR-2：output 路径迁移**（templates.py）
   - 改 `generate_outline` 和 `design_characters` 的 output
   - 给 `design_characters` 加 split
   - 同步 sample 项目 yaml
   - orchestrator 适配 split 读目录逻辑
   - 配套测试

3. **PR-3：文档**
   - 更新 `README.md`、`CLAUDE.md` 里的项目结构说明
   - 在 `docs/plan/novelforge-design.md` §5.5 标注 init 行为变更

## 9. 待用户决策的开放问题

1. **方案选 A / B / C？**（推荐 B）
2. **`design_characters` 是否真的要拆成多文件？** 如果暂时不想动 templates.py，方案 A 即可满足"目录骨架"诉求。
3. **种子文件用中文模板还是英文模板？** 当前 `samples/minimal-novel/` 用英文，但目标用户大概率中文写小说——可以双语并存或中文优先。
4. **`--skeleton-only` 是否需要？** 还是默认就不生成种子，加 `--with-seeds` 才生成？
5. **是否同步更新 `migrate` 命令？** 让迁移出来的项目也走新路径。

---

> 评审通过后按 §8 的步骤分 PR 实施。
