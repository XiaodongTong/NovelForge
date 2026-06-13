# Stage 契约协议 — v4 权威文档

> 状态：v4 已落地（2026-06-14）
> 关联规范：[`.cybervisor/artifacts/spec.md`](../../.cybervisor/artifacts/spec.md)
> 关联实施计划：[`.cybervisor/artifacts/plan.md`](../../.cybervisor/artifacts/plan.md)
> 取代：[`pipeline-customization.md`](./pipeline-customization.md)（v3 历史，已归档）

---

## 一、设计目标

把 NovelForge 从"按 id 顺序流转的命令式 pipeline"升级为**"基于产物契约的数据流 pipeline"**。

每个 stage 在 `novel-project.yaml` 里声明三件事：

1. **produces** — 我产出哪些文件，分别叫什么别名
2. **done_when** — 我怎么判定"这一轮做完了"（信号 + 检查项 + 上限）
3. **consumes** — 我从哪些上游 stage 接收数据

引擎据此自动完成：产物落盘 → 两层完成校验 → 数据通过 `ArtifactRegistry` 流转给下游 → prompt 用 `{{upstream.<id>.<alias>}}` 引用上游产物。**运行时是纯线性流水线**，没有路由跳转、没有回退、没有 NEEDS_REWRITE。

### 解决的三个结构性空洞

| 空洞 | v3 表现 | v4 解法 |
|---|---|---|
| A. 单产出假设 | `output` 是单字段，多产物要塞进一个文件或拆成伪 stage | `produces` 复数清单，每个含 path + alias + 可选 split |
| B. 完成判定缺失 | `invoke` 只看 exit code；写空文件也算成功 | 两层完成校验：模型自报信号 + 编排层独立 checks |
| C. 数据流硬编码 | `ContextAssembler` 按 stage id 加载固定 inputs | `consumes` + `ArtifactRegistry` + `{{upstream.*}}` 占位符 |

---

## 二、Stage 契约 schema

每个 stage 是 `pipeline.stages` 下的一个 YAML dict。

### 2.1 字段速查

| 字段 | 必填 | 默认 | 语义 |
|------|------|------|------|
| `id` | ✅ | — | pipeline 内唯一；`{{upstream.<id>.*}}` 用此引用 |
| `model` | ✅ | — | Claude 模型 ID（如 `claude-opus-4-7`） |
| `prompt` | ✅ | — | 提示词（文件路径或 inline 文本，引擎自动判定） |
| `produces` | ✅ | — | 产出文件清单（每个含 `path` + `alias` + 可选 `split`） |
| `done_when` | ❌ | 见 §2.3 | 完成判定（`completion_signal` + `checks` + `max_attempts`） |
| `consumes` | ❌ | `null` | 上游 stage 依赖（三态，见 §2.4） |
| `batch` | ❌ | `1` | 单次 stage 跑 N 个 item；`{{num}}` 占位符由 `ctx.batch` 渲染 |
| `on_failure` | ❌ | `pause` | max_attempts 耗尽时：`pause` / `skip` / `fail` |
| `enabled` | ❌ | `true` | `false` 时跳过 |

### 2.2 `produces[]` 形态

```yaml
produces:
  - path: output/summaries/plot.md         # 必填，落盘路径
    alias: outline                         # 必填，下游引用名（pipeline 内全局唯一）
    split: '^##\s+Chapter\s+(?P<num>\d+)'  # 选填，切分正则；与 batch 互斥
```

**Path 形态**：

| `path` 写法 | 引擎行为 | registry 存储 |
|---|---|---|
| `output/summaries/plot.md` | raw_output 整段写入 | `Path` |
| `output/review/x.json` | 必须可解析为 JSON；写入文件 | `Path` |
| `output/chapters/{{num:03d}}.md` | `batch: N` 驱动 N 次落盘 | `list[Path]`（长度 N） |
| `output/meta/{{name|slug}}.md` + `split` | 一次执行切分多文件 | `list[Path]` |

**`split` 规则**：

- 模板变量必须等于正则命名捕获组（`(?P<num>...)` 对应 `{{num}}`）
- 支持过滤器：`slug` / `lower` / `upper` / `:03d` 零填充
- `batch` 与 `split` 在 v1 **互斥**：同时设置 → `validate` 报错

### 2.3 `done_when` 形态

```yaml
done_when:
  max_attempts: 3                                  # 选填，默认 3
  completion_signal: "<promise>COMPLETE</promise>" # 选填，默认见上；设 null 关闭第一层
  mode: all                                        # 选填，all | any，默认 all
  checks:                                          # 选填，空数组 = 关闭第二层
    - kind: min_chars
      target: output/summaries/plot.md
      value: 500
    - kind: json_field
      target: output/review/chapter-review.json
      field: passed
```

#### 6 种 check kind

| kind | 字段 | 说明 |
|------|------|------|
| `exists` | `target` | 文件存在 |
| `min_chars` | `target`、`value: int` | 字符长度 ≥ value |
| `min_bytes` | `target`、`value: int` | 字节长度 ≥ value |
| `regex_match` | `target`、`pattern: str` | 内容含正则匹配 |
| `json_field` | `target`、`field: str`、`value?` | JSON 字段存在 / 等值 |
| `callable` | `target`、`callable: "module:func"` | 调用自定义 Python 函数 |

`mode: all`（默认）= 所有 checks 必须通过；`mode: any` = 任一通过即可。

### 2.4 `consumes` 三态

| 写法 | 语义 |
|------|------|
| 不写 / `null` | **全部已执行上游**（默认；下游能见到所有上游产物） |
| `[]` | **显式空**：无任何上游；引擎不注入任何上游产物 |
| `[a, b]` | **显式清单**：只绑定 a 和 b；其他上游被排除 |

`validate` 阶段必须区分 `null` 与 `[]`，不能把它们合并。

---

## 三、双层完成校验

```
┌─────────────────────────────────────────────────┐
│             GenericStage._run() 一次执行          │
├─────────────────────────────────────────────────┤
│  1. 渲染 prompt（含 EXECUTION_SUFFIX +           │
│     attempt_hint（第 N>1 轮） + COMPLETION_SUFFIX）│
│  2. adapter.invoke() — A 档错走原 retry 路径      │
│  3. 第一层：缺 completion_signal → raise          │
│     StageIncomplete                              │
│  4. 落盘 produces（支持复数 + split 内联）         │
│  5. 第二层：done_when.checks 不通过 → raise       │
│     VerifyFailed                                 │
│  6. 注册到 ArtifactRegistry                      │
│  7. return StageExecutionResult                  │
└─────────────────────────────────────────────────┘
                  │
                  │ StageIncomplete / VerifyFailed
                  ▼
┌─────────────────────────────────────────────────┐
│       Orchestrator C 档整轮重跑循环               │
├─────────────────────────────────────────────────┤
│  while attempts < max_attempts:                  │
│      try: _run()                                 │
│      except (StageIncomplete, VerifyFailed):     │
│          attempts += 1                           │
│          sleep(backoff)                          │
│          state.extra.stage_attempts[id] = attempts│
│          continue                                │
│      else: break                                 │
│  else:                                           │
│      _apply_on_failure(on_failure)               │
└─────────────────────────────────────────────────┘
```

**两层各自可关**：

- 关第一层：`done_when.completion_signal: null`
- 关第二层：`done_when.checks: []`（或 `done_when.mode` + 空 checks）

**attempt_hint（AC-17）**：第 N>1 轮重跑时，prompt 必须被注入一段机器可识别的 hint，至少含：

- 重试轮次（"第 N 轮"）
- 上一轮失败类型（"缺完成信号" / "校验失败：target=... kind=..."）

---

## 四、四档错误矩阵

| 档位 | 异常 | 触发场景 | 处置 |
|---|---|---|---|
| **A 档（基础设施）** | `RateLimited` / `WriteFailure` / `ContextOverflow` | 网络抖动、超时、429 | 内层 retry（指数退避）；**不**递增 C 档计数 |
| **B 档（模型格式）** | `SchemaInvalid` / `OutputParseError` | JSON 不符合 schema；split 正则不匹配 | 不重试，直接走 `on_failure` |
| **C 档（模型未完成）** | `StageIncomplete` / `VerifyFailed` | 缺完成信号 / checks 失败 | 整 stage 重跑（带 `attempt_hint`），最多 `max_attempts` |
| **D 档（语义）** | （外部 review gate） | 内容质量差 / OOC | 由专门 review stage 自检（独立 stage 表达） |

**关键不变量**：A 档 retry 与 C 档 retry 是**两个嵌套循环**，互不干扰。A 档失败不消耗 C 档 attempts；C 档失败不消耗 A 档 retry。

---

## 五、数据流：ArtifactRegistry

### 5.1 运行时结构

```python
# ArtifactRegistry 是 in-memory 字典，序列化到 state.yaml.extra.artifacts
{
    "generate_outline": {                    # stage_id
        "outline": Path("output/summaries/plot.md")      # 单产物 → Path
    },
    "write_chapter": {
        "chapter": [                                        # batch stage → list[Path]
            Path("output/chapters/001.md"),
            Path("output/chapters/002.md"),
            Path("output/chapters/003.md"),
        ]
    },
    "design_characters": {
        "characters": [                                     # split stage → list[Path]
            Path("output/meta/protagonist.md"),
            Path("output/meta/villain.md"),
        ]
    }
}
```

### 5.2 `{{upstream.*}}` 占位符族（AC-4）

下游 prompt 里引用上游产物，按 registry 存储形态分流：

| 上游形态 | 占位符写法 | 渲染结果 |
|---|---|---|
| 单产物（`Path`） | `{{upstream.<id>.<alias>}}` | 文件内容 |
| 单产物（`Path`） | `{{upstream.<id>.<alias>.path}}` | 路径字符串 |
| 批量 / split（`list[Path]`） | `{{upstream.<id>.<alias>[*]}}` | 多文件内容按 alias 顺序换行拼接 |
| 批量 / split（`list[Path]`） | `{{upstream.<id>.<alias>[*].path}}` | 路径列表（每行一个） |

**形态错配 → validate 报错**（强制显式 `[*]` 表达批量意图，避免静默取首/末元素）：

- 单产物 stage 用 `[*]` → 报错
- 批量产物 stage 用裸 `{{upstream.<id>.<alias>}}` 或裸 `.path` → 报错

### 5.3 跨 stage alias 唯一性（AC-18）

整个 `pipeline.stages` 内所有 `produces[].alias` 必须唯一。违反时报 `ConfigError`。

设计意图：禁止"重写覆盖上游 alias"。改写场景必须声明独立的下游 stage（如 `rewrite_chapter`），其 `produces` 用不同 alias（如 `chapter_v2`）或不同路径。

---

## 六、批量与切分

### 6.1 批量 stage（`batch: N`）

```yaml
- id: write_chapter
  batch: 3                              # 单次 stage 跑 3 个 item
  produces:
    - path: output/chapters/{{num:03d}}.md   # {{num}} 由 ctx.batch 渲染
      alias: chapter
```

引擎行为：

1. `Orchestrator._drive_batch` 驱动 N 次 `GenericStage._run()`
2. 每次 `StageContext.batch` 取不同值（1, 2, ..., N）
3. 每个 batch item 独立持有自己的 `done_when` attempt 计数
4. 某 item 耗尽 `max_attempts` → 触发该 stage 的 `on_failure`
5. registry 中 `chapter` alias 存为长度 N 的 `list[Path]`

**state 持久化形态**：

- 非批量 stage → `state.extra.stage_attempts[id]` 是 `int`
- 批量 stage → `state.extra.stage_attempts[id]` 是 `list[int]`，长度 = N

加载时若长度不匹配 `batch: N`，视为损坏 checkpoint，引导 `novelforge run --fresh`。

### 6.2 切分 stage（`produces[].split`）

```yaml
- id: design_characters
  produces:
    - path: output/meta/{{name|slug}}.md
      alias: characters
      split: '^##\s+(?P<name>[^\n]+)'    # 一次执行切分多文件
```

引擎行为：

1. `GenericStage._run()` 调一次 `adapter.invoke()`
2. `output_parser.parse()` 按 `split` 正则把 raw_output 切成 N 段
3. 每段按 `{{name|slug}}` 渲染文件名落盘
4. registry 中 `characters` alias 存为长度 N 的 `list[Path]`

### 6.3 v1 互斥

`batch` 与 `split` 在 v1 **互斥**：

- 同一 ProduceSpec 同时设置两者 → `validate` 报错
- 理由：`{{num}}`（batch 驱动）与命名捕获组（split 驱动）会引入命名冲突
- 未来若有真实需求，再设计组合形态

---

## 七、断点续传

### 7.1 持久化字段

`state.yaml.extra`：

```yaml
extra:
  stage_attempts:           # 每个 stage 当轮的 C 档重跑计数
    generate_outline: 0     # 非批量：int
    write_chapter: [0,0,0]  # 批量：list[int]，长度 = batch
  artifacts:                # ArtifactRegistry 序列化（D12）
    generate_outline:
      outline: output/summaries/plot.md
    write_chapter:
      chapter:
        - output/chapters/001.md
        - output/chapters/002.md
        - output/chapters/003.md
```

### 7.2 attempt 重置语义（AC-10）

- **同一进程内**：多轮 attempt 之间累加（attempts += 1）
- **`on_failure: pause` 触发后 resume**：对应 stage 的 attempt 计数重置为 **0**
  - 暂停 = 一轮终结；新一轮允许重新尝试
  - 批量 stage：整个 list 重置为长度 N 的零向量
- **`on_failure: skip / fail`**：不涉及续跑

---

## 八、删除的 v3 机制

| v3 概念 | v4 处置 |
|---|---|
| 10 个内置 stage 类（Python 子类） | 删除；唯一执行器是 `GenericStage` |
| `pipeline.template:` | 删除；`init` 模板只是脚手架，运行时不读 |
| `pipeline.stages_override:` | 删除 |
| `pipeline.scaffold_from:` | 删除 |
| 路由分支 `NEEDS_REWRITE` / `FUNDAMENTAL_ISSUE` / `APPROVED` | 删除；运行时纯线性，下一 stage = `stages[]` 中下一个 |
| `errors.FundamentIssue` / `ReviewLoopExceeded` | 删除；新模型用 `StageIncomplete` / `VerifyFailed` + `on_failure` |
| `novelforge migrate` 命令 | 删除；无外部用户，直接替换 |
| Deprecation warning 代码 | 删除 |
| `StageConfig.output` 单字段 | 删除；改为 `produces` 复数 |
| `StageConfig.split` 顶层字段 | 删除；改为 `produces[].split` 内联 |

`grep -r "NEEDS_REWRITE\|FUNDAMENTAL_ISSUE\|stages_override\|scaffold_from"` 在 `src/` 与 `tests/` 应无残留。

---

## 九、典型配置示例

### 9.1 三 stage 最小可跑（见 `samples/minimal-novel/`）

```yaml
pipeline:
  stages:
    - id: generate_outline
      model: claude-opus-4-7
      prompt: prompts/generate-outline.md
      produces:
        - path: output/summaries/plot.md
          alias: outline
      done_when:
        max_attempts: 3
        checks:
          - kind: min_chars
            target: output/summaries/plot.md
            value: 500

    - id: write_chapter
      model: claude-opus-4-7
      prompt: prompts/write-chapter.md
      consumes: [generate_outline]                # 显式清单
      produces:
        - path: output/chapters/{{num:03d}}.md
          alias: chapter
      batch: 1                                     # 简化示例：单 chapter
      done_when:
        max_attempts: 3
        checks:
          - kind: min_chars
            target: output/chapters/{{num:03d}}.md
            value: 1000

    - id: review_chapter
      model: claude-sonnet-4-6
      prompt: prompts/review-chapter.md
      consumes: []                                 # 显式空：无上游
      produces:
        - path: output/review/chapter-review.json
          alias: review
      done_when:
        checks:
          - kind: json_field
            target: output/review/chapter-review.json
            field: passed
```

### 9.2 改写场景（AC-18）

```yaml
pipeline:
  stages:
    - id: write_chapter
      produces:
        - path: output/chapters/{{num:03d}}.md
          alias: chapter                # 上游 alias

    - id: rewrite_chapter               # 改写为独立下游 stage
      consumes: [write_chapter, review_chapter]
      produces:
        - path: output/chapters-v2/{{num:03d}}.md
          alias: chapter_v2             # ❌ 不能用 chapter，会触发 AC-18 校验失败
```

---

## 十、验收标准映射

完整 AC 列表见 [`.cybervisor/artifacts/spec.md`](../../.cybervisor/artifacts/spec.md) §五。

| AC | 本文档对应章节 |
|---|---|
| AC-1 单 stage 多 produces | §2.2 / §5 |
| AC-2 第一层（缺信号 → StageIncomplete） | §三 step 3 |
| AC-3 第二层（checks 失败 → VerifyFailed） | §三 step 5 |
| AC-4 `{{upstream.*}}` 占位符族 | §5.2 |
| AC-5 默认 consumes = 所有上游 | §2.4 |
| AC-6 显式 consumes / 显式空 | §2.4 |
| AC-7 批量 stage | §6.1 |
| AC-8 split stage | §6.2 |
| AC-9 max_attempts 兜底 | §三 + §四 C 档 |
| AC-10 attempt 重置语义 | §7.2 |
| AC-11 completion_signal 可关闭 | §2.3 |
| AC-15 v3 残留删除 | §八 |
| AC-16 produces 跨记录校验 | §2.2 + §五 |
| AC-17 attempt_hint | §三 |
| AC-18 review / 重写模型 | §5.3 + §九.2 |

---

## 十一、决策摘要

| # | 决策 | 理由 |
|---|---|---|
| D1 | 不保留 v3 兼容 | 早期项目无用户（MEMORY 偏好）；留兼容会污染两层协议实现 |
| D2 | `produces` 复数代替 `output` 单字段 | 一个 stage 多产物是常态 |
| D3 | `split` 内联到 `produces[].split` | split 是 per-产物属性 |
| D4 | 两层协议各自可关 | 第一层轻量；第二层强制；不强制组合 |
| D5 | `consumes` 默认 = 所有上游 | 减少用户配置；用 token budget 兜底爆炸风险 |
| D6 | `StageIncomplete` / `VerifyFailed` 进 C 档整轮重跑集合 | 错误档位清晰 |
| D7 | `stage_attempts` 持久化到 `state.yaml` | 长流程必备 |
| D8 | `{{upstream.*}}` 按 registry 存储形态分流 + `[*]` 强制批量意图 | 避免静默取首/末 |
| D9 | `done_when.checks.kind` 限 6 种 | 覆盖常见场景；rubric / shell 推到未来 |
| D10 | `callable` 用 `"module:func"` 字符串 | 与 cybervisor.yaml 的 hook 风格一致 |
| D11 | 删除 v3 route 跳回 | 数据流管线不应有反向边 |
| D12 | `ArtifactRegistry` 序列化到 `state.extra.artifacts` | 与现有 `state.extra.*` 风格一致 |
| D13 | C 档重跑循环在 orchestrator 不在 GenericStage | 持久化 / 退避由 orchestrator 持有 |
| D14 | 批量 stage 由 orchestrator 驱动 N 次；item 独立 attempts | 失败隔离；state 形态按 batch 区分 |
