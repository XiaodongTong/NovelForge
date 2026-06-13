# Pipeline 自定义与 Stage 属性统一化方案（v3 历史，已归档）

> ⚠️ **本文档已归档为 v3 历史，仅供回溯设计动机参考。**
>
> v3 设计已被 [stage-contract.md](./stage-contract.md) 取代。当前运行时模型是 **v4 stage 契约协议**：
> - 删除了 8 字段方案中的 `output` / `split` 顶层字段，改为 `produces` 复数 + `produces[].split` 内联
> - 删除了 `route` 跳 stage id 的路由机制，改为纯线性流水线
> - 删除了 `template` / `stages_override` / `scaffold_from` 字段
> - 删除了 10 个内置 stage 类，唯一执行器是 `GenericStage`
> - 新增了 `done_when`（双层完成校验）与 `consumes`（数据流依赖）两个 stage 字段
> - 新增了 `ArtifactRegistry` 与 `{{upstream.*}}` 占位符
>
> **请勿按本文档的 yaml 形态编写新配置**；以 [stage-contract.md](./stage-contract.md) 为准。
>
> ---
>
> 原始元信息（v3 阶段）：
>
> 状态：v4 已落地（2026-06-13）
> 作者：NovelForge 设计组
> 日期：2026-06-11（v4 落地：2026-06-13）
> 关联：[novelforge-design.md](./novelforge-design.md) §3.1 / §3.2
> v3 → v4 变更：**删除 `rewind_to` 字段**。审查 / 路由不再由配置字段决定，而是由 Claude 返回 JSON 里的 `route` 直接指定下一个 `stage.id`。也就是说：**route 跳的是 id**。

---

## 一、动机

当前 `novel-project.yaml` 中的 `pipeline` 配置只接受两种形态：

```yaml
pipeline:
  template: "long-epic"
  stages_override: null
```

存在四个核心问题：

1. **粗粒度**：用户只能「选模板」或「列 stage id」，没有任何 stage 内部参数可改写。
2. **隐式语义**：「用什么模型、提示词是什么、产物写到哪里、审查失败后跳哪里」全部硬编码在 Python 里。
3. **template 越权**：既是「快速生成默认配置」的语义，又是「运行时驱动 stage」的事实数据源。
4. **字段一旦放开就会爆炸**：配置越灵活，越容易变成 30+ 字段的大表，后期理解成本很高。

> **本方案的核心主张（v4 取舍）**：
> - **stage 是一次 Claude 调用的纯声明**：模型 + 提示词 + 输出，仅此而已。
> - **8 个扁平字段封顶**，无嵌套子块。
> - **路径即类型**：`output` 路径的扩展名 / 占位符自描述输出形态（文本 / JSON / split）。
> - **route 跳 id**：如果 JSON 输出里有 `route` 字段，值必须是某个 `pipeline.stages[].id`，orchestrator 直接跳到该 stage。
> - **template 仅是脚手架**：`init` 时展开成 yaml，运行时不读。

---

## 二、设计原则

| 原则 | 含义 |
|------|------|
| **8 字段封顶** | 一个 stage 的全部字段在一屏内看完；新增字段必须先砍一个老字段或写明硬理由。 |
| **扁平优先** | 凡能扁平不嵌套；不用 `prompt.file` / `output.format` / `review.*` 这种深层结构。 |
| **路径即类型** | 输出形态由 `output` 路径推断：含 `{{x}}` → split；以 `.json` 结尾 → JSON；否则 text。 |
| **route 即 stage id** | `route` 不再是 `APPROVED / NEEDS_REWRITE` 这类语义枚举，而是直接填下一个要执行的 `stage.id`。 |
| **自然顺序是默认路由** | JSON 里没有 `route`、或非 JSON 输出，默认执行下一个 stage。 |
| **没有内置 prompt** | 提示词必填、由用户给出；引擎不提供默认 prompt。 |
| **模板即数据** | `template` 在 `init` 时一次性物化为 stages，运行时只读 `pipeline.stages`。 |

---

## 三、当前 10 个内置 Stage 的属性归纳

| stage_id | 落盘路径 | 切分正则? | 模型 | route 行为 | batch |
|----------|---------|----------|------|------------|-------|
| generate_outline | `output/summaries/outline-{{num:03d}}.md` | 是 | write | 默认 next | 50 |
| review_outline | `output/review/outline-review.json` | — | review | Claude 返回 `route: design_characters` 或 `route: generate_outline` | — |
| design_characters | `output/meta/{{name|slug}}.md` | 是 | write | 默认 next | — |
| review_characters | `output/review/characters-review.json` | — | review | Claude 返回 `route: simulate_plot` 或 `route: design_characters` | — |
| simulate_plot | `output/summaries/plot-simulation.md` | — | review/write | 默认 next | — |
| review_simulation | `output/review/simulation-review.json` | — | review | Claude 返回 `route: write_chapter` 或 `route: simulate_plot` | — |
| write_chapter | `output/chapters/{{num:03d}}-{{title|slug}}.md` | 是 | write | 默认 next | 3 |
| review_chapter | `output/review/chapter-review.json` | — | review | Claude 返回 `route: write_chapter` 或下一个检查 stage id | — |
| full_consistency_check | `output/review/consistency-report.md` | — | review | 默认 next | — |
| final_polish | `output/review/final-polish-notes.md` | — | write | 默认 end | — |

**结论**：现有 10 个 stage 的全部差异，用 `model / prompt / output / split / batch` 加上 JSON 里的 `route` 就能表达。不需要 `type`、`uses`、`rewind_to`、`review.*`。

---

## 四、新 Schema：8 个扁平字段

```yaml
pipeline:
  scaffold_from: long-epic           # 选填，仅提示信息；运行时不读
  stages:
    - id: write_chapter              # ① 必填，pipeline 内唯一
      model: claude-opus-4-7         # ② 必填
      prompt: prompts/write.md       # ③ 必填（路径或 inline 自动判定）
      output: 'output/chapters/{{num:03d}}-{{title|slug}}.md'   # ④ 必填
      split: '^#\s+Chapter\s+(?P<num>\d+)\s*[-–—:]\s*(?P<title>.+?)$'  # ⑤ output 含 {{...}} 时必填
      batch: 3                       # ⑥ 选填，默认 1
      on_failure: pause              # ⑦ 选填，默认 pause
      enabled: true                  # ⑧ 选填，默认 true
```

### 4.1 8 字段速查

| # | 字段 | 必填 | 默认 | 一句话语义 |
|---|------|------|------|-----------|
| ① | `id` | ✅ | — | pipeline 内唯一；`route` 只能跳这些 id |
| ② | `model` | ✅ | — | 调 Claude 用的模型 ID |
| ③ | `prompt` | ✅ | — | 提示词。字符串：能 stat 到的单行路径当文件读，否则按 inline 文本 |
| ④ | `output` | ✅ | — | 落盘路径。含 `{{x}}` → split；以 `.json` 结尾 → JSON；否则 text |
| ⑤ | `split` | ⚠️ | — | 切分正则。仅当 `output` 含 `{{x}}` 时必填 |
| ⑥ | `batch` | ❌ | `1` | 单次调用产出的单位数（章节数 / 大纲批次数等） |
| ⑦ | `on_failure` | ❌ | `pause` | 失败处置：`pause` / `skip` / `fail` |
| ⑧ | `enabled` | ❌ | `true` | `false` 时跳过该 stage |

### 4.2 几条「约定优于配置」的规则

#### Rule 1：`prompt` 形态自动判定

引擎按以下顺序判定 prompt 字符串：

1. 包含换行符 `\n` → inline
2. 否则 `(project_root / prompt).exists()` 为真 → 文件路径
3. 否则 → inline

#### Rule 2：`output` 路径自描述形态

| `output` 写法 | 形态 | 引擎行为 |
|--------------|------|---------|
| `output/summaries/plot.md` | text | raw_output 整段写入 |
| `output/review/x.json` | JSON | raw_output 必须可解析为 JSON；写入文件 |
| `output/chapters/{{num:03d}}-{{title|slug}}.md` | split | 按 `split` 正则切分 raw_output，每段按模板渲染文件名落盘 |
| `output/review/chunks/{{num}}.json` | **非法** | `.json` 后缀与 `{{x}}` 占位符互斥；`validate` 阶段直接报错（A15） |

#### Rule 3：split 模板变量 = 正则命名捕获组

`output` 模板里的 `{{num:03d}}`、`{{title|slug}}` 必须有同名的 `(?P<num>...)`、`(?P<title>...)` 捕获组。

支持的过滤器：`slug` / `lower` / `upper`。

#### Rule 4：`route` 是 JSON 输出里的下一跳 id

如果某个 stage 的 `output` 是 `.json`，引擎会解析 raw_output。若 JSON 中存在：

```json
{ "route": "write_chapter" }
```

则 `route` 必须满足：

1. 值是字符串
2. 值等于某个 `pipeline.stages[].id`
3. 目标 stage 必须 `enabled: true`

orchestrator 直接把 cursor 跳到该 id。

如果 JSON 中**没有** `route` 字段，则按自然顺序执行下一个 stage。

如果 `route` 指向未知 id、禁用 stage、或造成超过 `execution.max_review_iterations` 的循环，则按 `on_failure` 处置；默认 `pause`。

#### Rule 5：怎么表达 APPROVED / NEEDS_REWRITE / FUNDAMENTAL_ISSUE

v4 不再让 `route` 填语义枚举，而是让 prompt 把判断结果**翻译成目标 stage id**：

```text
请审查章节并返回 JSON。
你必须根据审查结论填写 route：
- 通过：route = "full_consistency_check"
- 需要重写：route = "write_chapter"
- 根本性问题：route = "human_review"
```

对应 pipeline 里可以显式放一个人工处理 stage：

```yaml
- id: human_review
  model: claude-sonnet-4-6
  prompt: |
    汇总前面审查发现的问题，生成一份给人工作者看的决策说明。
    {{include: output/review/*.json}}
  output: output/review/human-review.md
  on_failure: fail
```

也可以不配置 `human_review`，让 prompt 在根本性问题时返回一个未知 id；引擎会 pause。但推荐显式配置 `human_review`，这样 pipeline 结构更清楚。

#### Rule 6：prompt 内 3 类占位符

| 占位符 | 含义 |
|--------|------|
| `{{novel.<key>}}` | 取自 `novel:` 配置（title / genre / target_chapters / ...） |
| `{{ctx.<key>}}` | 运行时上下文（stage_id / batch / chapter_index / iteration） |
| `{{include: <path-or-glob>}}` | 嵌入项目内文件内容（自动登记为 ContextAssembler 依赖） |

### 4.3 v3 → v4 字段对比

| v3 字段 | v4 处置 |
|---------|--------|
| `rewind_to` | ❌ 删除。由 JSON `route` 直接填写目标 `stage.id` |
| `route = APPROVED / NEEDS_REWRITE / FUNDAMENTAL_ISSUE` | ❌ 删除。`route` 改为目标 stage id |
| `id / model / prompt / output / split / batch / on_failure / enabled` | ✅ 保留 |

净效果：**v3 的 9 个字段 → v4 的 8 个字段**；路由从「配置字段 + 语义枚举」改成「JSON 直接跳 id」。

---

## 五、Template 的新角色：脚手架（Scaffold）

### 5.1 行为变更

| 操作 | 旧行为 | 新行为 |
|------|-------|-------|
| `novelforge run --config x.yaml` | 读 `pipeline.template` 运行时绑定 | 仅读 `pipeline.stages`；旧 `template` 字段被忽略并 warn |
| `novelforge init --template long-epic` | 不存在 | 把 long-epic 展开为完整 8 字段 stage 数组写入 yaml，同时把 prompt 模板写入 `prompts/` |
| `novelforge validate` | 校验 template 与 override | 按 v4 schema 校验 stages 数组 |

### 5.2 内置模板（仅 init-time 数据源）

```python
LONG_EPIC: list[dict] = [
    {
        "id": "generate_outline",
        "model": "claude-opus-4-7",
        "prompt": "prompts/generate-outline.md",
        "output": "output/summaries/outline-{{num:03d}}.md",
        "split": r"^##\s+Chapter\s+(?P<num>\d+)",
        "batch": 50,
    },
    {
        "id": "review_outline",
        "model": "claude-sonnet-4-6",
        "prompt": "prompts/review-outline.md",
        "output": "output/review/outline-review.json",
    },
    # review-outline.md 内约定：
    # - 通过 → route: design_characters
    # - 需要重写 → route: generate_outline
    # - 根本问题 → route: human_review
]
```

---

## 六、示例配置

### 6.1 最小可跑（两个 stage）

```yaml
novel:
  title: "Demo"
  genre: "玄幻"
  target_chapters: 1
  words_per_chapter: [800, 1500]
  style: "demo"
  seeds: [outline/premise.md, outline/world.md]
  constraints: [CLAUDE.md]

pipeline:
  stages:
    - id: write_chapter
      model: claude-opus-4-7
      prompt: |
        写《{{novel.title}}》第 {{ctx.chapter_index}} 章。
        前提：{{include: outline/premise.md}}
        世界观：{{include: outline/world.md}}
        规则：{{include: CLAUDE.md}}
        输出以 `# Chapter N - 标题` 开头，800–1500 字。
      output: 'output/chapters/{{num:03d}}-{{title|slug}}.md'
      split: '^#\s+Chapter\s+(?P<num>\d+)\s*[-–—:]?\s*(?P<title>.+?)$'

    - id: review_chapter
      model: claude-sonnet-4-6
      prompt: |
        审查以下章节，返回 JSON：
        {
          "route": "write_chapter" 或 "done",
          "findings": ["..."],
          "summary": "..."
        }

        route 规则：
        - 章节达标：route = "done"
        - 需要重写：route = "write_chapter"

        {{include: output/chapters/*.md}}
      output: output/review/chapter-review.json

    - id: done
      model: claude-sonnet-4-6
      prompt: |
        汇总本次运行结果，输出简短完成报告。
        {{include: output/review/chapter-review.json}}
      output: output/review/done.md
```

### 6.2 完整一点：审查通过跳后续 stage，失败跳回写作 stage

```yaml
pipeline:
  stages:
    - id: generate_outline
      model: claude-opus-4-7
      prompt: prompts/generate-outline.md
      output: 'output/summaries/outline-{{num:03d}}.md'
      split: '^##\s+Chapter\s+(?P<num>\d+)'
      batch: 50

    - id: review_outline
      model: claude-sonnet-4-6
      prompt: |
        审查大纲，返回 JSON：
        - 通过：{"route": "write_chapter", "findings": []}
        - 需要重写：{"route": "generate_outline", "findings": ["..."]}
        - 需要人工：{"route": "human_review", "findings": ["..."]}
        {{include: output/summaries/*.md}}
      output: output/review/outline-review.json

    - id: write_chapter
      model: claude-opus-4-7
      prompt: prompts/write.md
      output: 'output/chapters/{{num:03d}}-{{title|slug}}.md'
      split: '^#\s+Chapter\s+(?P<num>\d+)\s*[-–—:]?\s*(?P<title>.+?)$'
      batch: 3

    - id: human_review
      model: claude-sonnet-4-6
      prompt: |
        汇总需要人工介入的问题。
        {{include: output/review/*.json}}
      output: output/review/human-review.md
      on_failure: fail
```

### 6.3 暂时禁用一个 stage

```yaml
- id: final_polish
  model: claude-opus-4-7
  prompt: prompts/polish.md
  output: output/review/final-polish-notes.md
  enabled: false
```

---

## 七、向后兼容与迁移

### 7.1 加载策略（v0 → v4）

```
读 yaml →
  if 'stages' in pipeline:
      按 v4 schema 校验
  elif 'template' in pipeline:
      DeprecationWarning
      内部把 template 展开成 v4 stages（含完整 prompt/output 字段）
      继续往下走
  else:
      默认展开 long-epic
```

### 7.2 迁移命令

```
novelforge migrate --config novel-project.yaml [--write]
```

读旧 yaml + Python 默认 prompt，渲染出 v4 yaml + `prompts/*.md` 文件；`--write` 时原地覆盖（旧文件备份为 `.bak`）。

---

## 八、对引擎内部的影响

| 模块 | 改动 |
|------|------|
| `config.py` | 新增 `StageConfig`（8 字段平铺）；扩展 `_parse_pipeline` 支持新旧两种形态 |
| `stages/` | 删除所有内置 stage 类；Python 侧不再有「stage 实现」概念 |
| `stages/generic.py`（新） | 唯一一个 `GenericStage`，执行通用流程 |
| `claude/context.py` | 新增 `PromptRenderer`：识别 `{{novel.*}}` / `{{ctx.*}}` / `{{include:}}` |
| `claude/output_parser.py`（新） | 按 `output` 路径推断形态：text / JSON / split |
| `orchestrator.py` | 路由逻辑改为：JSON 中存在 `route` 就跳对应 stage id；否则自然 next |
| `review/gate.py` | 可删除，或退化为 JSON 解析工具 |
| `cli.py` | 新增 `init` 与 `migrate`；`validate` 升级到 v4 schema |
| `templates.py`（新） | 模板数据源（含 prompt 模板原文） |

**核心不变量**：`Checkpoint` 文件格式、`state.yaml` schema 保持不变。

**核心变化**：所有 stage 走同一段通用代码；路由从 JSON `route` 直接跳 `stage.id`。

---

## 九、验收标准

- [ ] **A1**：旧 yaml（仅 `template`）能加载、跑通 sample、打 1 条 DeprecationWarning
- [ ] **A2**：v4 yaml 跑 sample，产物语义与旧版等价
- [ ] **A3**：改单 stage 的 `model` / `prompt` / `output` / `split` 后运行时立即生效
- [ ] **A4**：`enabled: false` 的 stage 被完全跳过；status 标记 `skipped`
- [ ] **A5**：JSON 输出含 `route: <stage_id>` 时，orchestrator 跳到该 id
- [ ] **A6**：JSON 输出无 `route` 时，orchestrator 执行自然下一个 stage
- [ ] **A7**：`route` 指向未知 id 或 disabled stage 时，按 `on_failure` 处置（默认 pause）
- [ ] **A8**：`route` 形成循环并超过 `execution.max_review_iterations` 时，按 `on_failure` 处置（默认 pause）
- [ ] **A9**：`output` 以 `.json` 结尾时，模型返回非 JSON 抛 `SchemaInvalid`
- [ ] **A10**：`output` 含 `{{num}}` 但缺 `split` 字段 → `validate` 阶段报错
- [ ] **A11**：`split` 正则未匹配任何段 → 按 `on_failure` 处置
- [ ] **A12**：`{{include: <glob>}}` 正确拼接多文件、超预算时 ContextAssembler 裁剪并 warn
- [ ] **A13**：`novelforge init --template long-epic` 生成的 yaml 与旧版 long-epic 功能等价且可直接跑
- [ ] **A14**：`novelforge migrate` 把旧 yaml + 内置 prompt 渲染为新 yaml + `prompts/*.md`

---

## 十、开放问题

1. **是否需要一个保留 id 表达结束 / 暂停？** v4 推荐显式配置 `done` / `human_review` stage，而不是引入 `__end__` / `__pause__` 这种伪 id。
2. **stage 间显式依赖**：是否引入 `needs: [...]` 让 DAG 成为可能？
3. **并行执行**：`parallel: true` + 全局 token 节流器。
4. **stage 级 retry 覆盖是否需要恢复**：现在统一用全局，若有 stage 频繁失败再加。
5. **prompt 模板引擎**：内置简版占位符 vs Jinja2。

---

## 十一、决策摘要

> 全部决策已 v4 落地（2026-06-13），对应实现见 `src/novelforge/config.py` (`StageConfig`)、`src/novelforge/stages/generic.py` (`GenericStage`)、`src/novelforge/templates.py` (内置模板数据源)、`src/novelforge/cli.py` (`init` / `migrate`)。

1. ✅ **8 字段封顶**：`id / model / prompt / output / split / batch / on_failure / enabled`
2. ✅ **删除 type / uses / rewind_to**
3. ✅ **route 跳 stage.id**：JSON 里 `route` 的值就是下一跳 stage id
4. ✅ **无 route = 自然 next**
5. ✅ **路径即类型**：`output` 路径自描述形态，不增设 format 字段
6. ✅ **prompt 必填、无内置 fallback**
7. ✅ **template 降级为脚手架**：仅 `init` / `migrate` 用
8. ✅ **Python 侧只剩一个 `GenericStage`**（v3 旧 stage 类以兼容性 stub 形式保留，阶段 E 后续 PR 删除）
