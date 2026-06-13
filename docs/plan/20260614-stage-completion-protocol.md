# Stage 契约协议 设计方案 (v3)

> **日期**：2026-06-14
> **状态**：设计待评审（design draft）
> **范围**：NovelForge 引擎 stage 执行层（`src/novelforge/`）
> **关联**：[pipeline-customization.md](./pipeline-customization.md)、[novelforge-design.md](./novelforge-design.md)、[cybervisor.yaml](../../cybervisor.yaml) 的 `contract:` 设计

## 修订说明

- **v1** 只提出了"完成信号 + verify_fn"两道补丁，未触及 stage 之间的数据流。
- **v2** 升级为 stage 契约模型，但保留了向后兼容（`output` 作为语法糖、v3 stage 类兼容 shim 等）。
- **v3**（当前版本）按"早期项目不考虑兼容"原则，**删除所有兼容包袱**，直接按最新设计落地。

## 一、背景与三个核心需求

### 1.1 现状缺陷

NovelForge 当前的 stage 执行层有三个空洞：

| 空洞 | 表现 | 代码位置 |
|---|---|---|
| A. 单产出假设 | `StageConfig.output` 是单字段，但实际一个 stage 可能产出多个文件（大纲 + 进度 + 人物表） | `config.py:71` |
| B. 完成判定缺失 | `ClaudeAdapter.invoke` 只看 exit code；模型写空文件、写残、没写都视为成功 | `claude/adapter.py:258` |
| C. 数据流硬编码 | `ContextAssembler` 按 stage id 加载固定 inputs，没有"上游 produces → 下游 consumes"的自动绑定 | `claude/context.py:655` |

### 1.2 三个核心需求（评审原话）

1. **配置本阶段会产生哪些文件**（生产者契约）
2. **检查产出内容是否存在，决定能否结束本阶段**（自校验契约）
3. **产出内容自动带到下一阶段**（消费者契约 / 数据流）

三条合起来等价于：把 NovelForge 的 stage 模型从"按 id 顺序流转的命令式 pipeline"升级为"**基于产物契约的数据流 pipeline**"。

### 1.3 已有的同类设计

- `cybervisor.yaml` 的上层 cybervisor 流水线已经有 `contract.inputs / contract.outputs` 声明式契约（globs 列表）。本方案把同样的设计下沉到 NovelForge 引擎层。
- `/Users/russell/workingspace/blink/src/blink/loop/runner/claude.py` 的 `verify_fn` + `COMPLETION_SUFFIX` 提供"完成判定"的运行时实现参考。

---

## 二、参考实现

### 2.1 blink — 完成信号 + verify_fn 主动校验

```python
COMPLETION_SUFFIX = "...output <promise>COMPLETE</promise> when fully done..."

def run_claude(prompt, cwd, max_retries, verify_fn):
    for attempt in 1..max_retries:
        result = subprocess.run(...)              # 1. 跑 Claude
        if result.returncode != 0: continue       # 2. 基础设施错→重试
        if "<promise>COMPLETE</promise>" not in result.stdout:
            continue                              # 3. 缺完成信号→重跑
        if verify_fn(cwd): return True            # 4. 产物校验通过→成功
    return False                                  # 5. 全部轮次耗尽
```

**关键**：把"是否完成"的判定权从"看 exit code"换到"看模型自报信号 + 编排层独立校验产物"。

### 2.2 cybervisor.yaml — 声明式 stage 契约

```yaml
- name: Implement
  contract:
    inputs:
      - .cybervisor/artifacts/spec.md
      - .cybervisor/artifacts/plan.md
    outputs:
      - src/novelforge/...
```

**关键**：每个 stage 显式声明"我消费什么、我产出什么"，编排层据此组装上下文。

---

## 三、方案设计 — Stage 契约模型

### 3.1 契约的四个组成部分

```
┌─ Stage Contract ────────────────────────────────────────────────┐
│                                                                 │
│   produces   本阶段产出哪些文件（必填，复数）                       │
│              ↓ 决定 done_when 的校验目标                            │
│              ↓ 注册到 ArtifactRegistry 供下游引用                   │
│                                                                 │
│   done_when  完成判定：基于 produces 的检查规则                     │
│              ├─ 第一层: completion_signal（轻量，模型自报）         │
│              └─ 第二层: checks（强制，编排层独立校验）              │
│                                                                 │
│   consumes   本阶段从哪些上游 stage 接收 produces                  │
│              ├─ 显式: consumes: [generate_outline, ...]           │
│              └─ 默认: 所有已执行的上游 stage                       │
│                                                                 │
│   max_attempts  完成判定不通过时的整轮重跑上限                     │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 3.2 新的 `StageConfig`（直接替换旧定义）

```python
@dataclass(frozen=True)
class ProduceSpec:
    """一个产物的声明。"""
    path: str                            # 支持 {{num}} / {{batch}} 占位符
    alias: str                           # 必填，给下游 {{upstream.X.alias}} 引用
    split: Optional[str] = None          # 取代旧 StageConfig.split；split_regex 内联到产物

@dataclass(frozen=True)
class CheckSpec:
    """done_when 里的一条校验规则。"""
    target: str                          # 支持 {{num}} / {{batch}} 占位符
    kind: Literal[
        "exists",        # 文件存在且非空
        "min_chars",     # 字符数下限
        "min_bytes",     # 字节数下限
        "regex_match",   # 内容匹配正则
        "json_field",    # JSON 字段值匹配
        "callable",      # 业务自定义 Python 函数
    ]
    value: Optional[int] = None          # for min_chars / min_bytes
    pattern: Optional[str] = None        # for regex_match
    field: Optional[str] = None          # for json_field
    equals: Optional[Any] = None         # for json_field
    callable: Optional[str] = None       # "module:func" for callable

@dataclass(frozen=True)
class DoneWhenSpec:
    """完成判定：两层协议。"""
    completion_signal: Optional[str] = "<promise>COMPLETE</promise>"
    max_attempts: int = 3
    mode: Literal["all", "any"] = "all"
    checks: tuple[CheckSpec, ...] = ()

@dataclass(frozen=True)
class StageConfig:
    """v4 stage 契约（取代旧 8 字段定义）。"""
    id: str
    model: str
    prompt: str
    produces: tuple[ProduceSpec, ...]    # 必填，至少一个
    done_when: DoneWhenSpec = field(default_factory=DoneWhenSpec)
    consumes: Optional[tuple[str, ...]] = None    # None = 所有上游
    batch: int = 1
    on_failure: Literal["pause", "skip", "fail"] = "pause"
    enabled: bool = True
```

**与旧 `StageConfig` 的差异**：
- 删除 `output` 单字段 → 用 `produces` 复数代替
- 删除 `split` 顶层字段 → 并入 `produces[].split`
- 新增 `done_when`、`consumes`

### 3.3 yaml 完整形态

```yaml
pipeline:
  stages:
    - id: generate_outline
      model: claude-opus-4-7
      prompt: prompts/generate-outline.md
      
      produces:                                   # 必填
        - path: output/outline/plot.md
          alias: plot
        - path: output/outline/tracking.md
          alias: tracking
        - path: output/outline/cast.md
          alias: cast
      
      done_when:
        max_attempts: 3
        mode: all
        checks:
          - target: output/outline/plot.md
            kind: min_chars
            value: 5000
          - target: output/outline/tracking.md
            kind: regex_match
            pattern: "已完成.*章"
    
    - id: write_chapter
      model: claude-opus-4-7
      prompt: prompts/write-chapter.md
      
      consumes: [generate_outline, design_characters]    # 显式声明上游
      
      produces:
        - path: "output/chapters/{{num}}.md"
          alias: chapter
          split: "(?P<num>\\d{3})"               # split 内联到 produces
      
      done_when:
        checks:
          - target: "output/chapters/{{num}}.md"
            kind: min_chars
            value: 2500
      
      batch: 3
```

### 3.4 完成判定的两层协议

```
                  StageConfig.done_when
                          │
                          ▼
   ┌──────────────────────────────────────────────────────────┐
   │  for attempt in 1..max_attempts:                         │
   │                                                          │
   │  ┌────────────────────────────────────────────────────┐  │
   │  │ 1. render prompt                                    │  │
   │  │    + EXECUTION_SUFFIX  ("立即执行,不要只描述")       │  │
   │  │    + COMPLETION_SUFFIX (<promise>COMPLETE</promise>)│  │
   │  │    + attempt_hint      (第 N 轮,上次没完成)          │  │
   │  │                                                     │  │
   │  │ 2. adapter.invoke()         ← 基础设施错仍走原重试   │  │
   │  │                                                     │  │
   │  │ 3. ★ 第一层：completion_signal 检测                 │  │
   │  │    ├─ 缺信号  → raise StageIncomplete               │  │
   │  │    └─ 有信号  ↓                                    │  │
   │  │                                                     │  │
   │  │ 4. _persist()  落盘所有 produces (复用现有 parser)  │  │
   │  │                                                     │  │
   │  │ 5. ★ 第二层：done_when.checks 校验                  │  │
   │  │    ├─ 不通过 → raise VerifyFailed                   │  │
   │  │    └─ 通过   ↓                                    │  │
   │  │                                                     │  │
   │  │ 6. 注册产物到 ArtifactRegistry (供下游 consumes)    │  │
   │  │                                                     │  │
   │  │ 7. return StageExecutionResult ✓                    │  │
   │  └────────────────────────────────────────────────────┘  │
   │                                                          │
   │  except (StageIncomplete, VerifyFailed):                 │
   │    sleep(backoff); continue                              │
   │                                                          │
   │  return None  # 全部轮次耗尽                              │
   └──────────────────────────────────────────────────────────┘
                          │
                          ▼
       max_attempts 耗尽 → on_failure (pause/skip/fail)
```

**两层协议的分工**：
- 第一层（completion_signal）：**轻量、模型侧**。模型自报"我做完了"，拦截"嘴上没说"的情况。
- 第二层（done_when.checks）：**强制、编排层**。引擎独立验证产物文件，拦截"嘴上说做完了但其实没做"的情况。

### 3.5 数据流：produces 如何流入下游

引擎引入 `ArtifactRegistry`（运行时内存对象，配合 `state.yaml` 持久化）：

```python
artifact_registry = {
    "generate_outline": {
        "plot":     Path("output/outline/plot.md"),
        "tracking": Path("output/outline/tracking.md"),
        "cast":     Path("output/outline/cast.md"),
    },
    "write_chapter": {
        "chapter": [
            Path("output/chapters/001.md"),
            Path("output/chapters/002.md"),
            Path("output/chapters/003.md"),
        ],  # 批量 stage 的产物是列表
    },
}
```

`ContextAssembler.assemble(stage_id)` 的逻辑（**完全重写，不保留老 inputs**）：

```
1. 读 stage.consumes（默认 = 所有已执行的上游 stage_id）
2. 对每个上游 stage：
   - 取 artifact_registry[upstream_id] 的所有 alias
   - 把对应文件路径加入 include_files
3. 渲染 prompt 时支持占位符：
   {{upstream.generate_outline.plot}}        → 文件内容
   {{upstream.generate_outline.plot.path}}   → 文件路径
   {{upstream.write_chapter.chapter[*]}}     → 批量产物列表
```

### 3.6 批量 stage 与 split stage 的处理

| 场景 | produces 写法 | 落盘行为 | ArtifactRegistry 存储 |
|---|---|---|---|
| 单产物 | `produces: [{path: a.md, alias: a}]` | 单文件 | `dict[alias, Path]` |
| 批量 stage | `produces: [{path: "ch{{num}}.md", alias: ch}]` + `batch: 3` | 多次执行，每次一个文件 | `dict[alias, list[Path]]` |
| split stage | `produces: [{path: "ch{{num}}.md", alias: ch, split: regex}]` | 一次执行，按 regex 切分多文件 | `dict[alias, list[Path]]` |

`done_when.checks` 里的 `target` 也支持 `{{num}}` 占位符展开，对每个生成的文件分别校验。

### 3.7 四档错误的最终矩阵

| 错误档 | 例子 | 处置 | 来源 |
|---|---|---|---|
| A. 基础设施错 | 429 / 5xx / CLI 超时 | 同 prompt 重试 N 次（指数退避） | 现有保留 |
| B. 模型格式错 | JSON 不符合 schema | 不重试，走 on_failure | 现有保留 |
| **C. 模型未完成** | **没写产物 / 写残 / 缺完成信号 / done_when 校验失败** | **整轮重跑，带 attempt_hint** | **本方案新增** |
| D. 模型语义错 | 内容质量差 / OOC | review gate 有限循环 | 现有保留 |

---

## 四、改造点清单

### 4.1 新增模块

| # | 文件 | 内容 |
|---|---|---|
| 1 | `errors.py` 扩展 | 新增 `StageIncomplete`、`VerifyFailed`（继承 `NovelForgeError`） |
| 2 | `artifact_registry.py`（新） | `ArtifactRegistry` 类：`register(stage_id, alias, path)` / `get(upstream_id, alias) -> Path \| list[Path]` / 持久化到 `state.yaml` |
| 3 | `verify.py`（新） | `CheckSpec` / `DoneWhenSpec` 数据类 + `run_done_when(spec, files, project_root, placeholders) -> CheckResult` |

### 4.2 重写模块（直接替换，不留兼容）

| # | 文件 | 改动 |
|---|---|---|
| 4 | `config.py` | **重写** `StageConfig`：删除 `output` 单字段、删除 `split` 顶层字段；新增 `produces` / `done_when` / `consumes` 三个核心字段 |
| 5 | `claude/adapter.py` | `StageResult.completion_signal: bool`；`invoke()` 自动追加 EXECUTION_SUFFIX + COMPLETION_SUFFIX（参数 `append_suffix=True`，默认开）；`MockClaudeAdapter` 默认吐完成信号 |
| 6 | `claude/context.py` | **重写** `ContextAssembler`：直接依赖 `ArtifactRegistry`，删除老 `inputs` 解析路径；新增 `{{upstream.*}}` 占位符支持 |
| 7 | `stages/generic.py` | `_run()` 重构：① 落盘 `produces`（支持复数 + split 内联） ② 注册到 ArtifactRegistry ③ 执行 `done_when.checks`（两层协议） |
| 8 | `orchestrator.py` | `_execute_with_retry` 把 `StageIncomplete` / `VerifyFailed` 加入"整轮重跑"集合；`state.extra.stage_attempts` 持久化（断点续传）；**删除** v3 兼容路径（NEEDS_REWRITE / FUNDAMENTAL_ISSUE 等老 route 值） |

### 4.3 删除模块（直接清理）

| # | 文件 | 操作 |
|---|---|---|
| 9 | `src/novelforge/stages/design_characters.py` | 删除 |
| 10 | `src/novelforge/stages/final_polish.py` | 删除 |
| 11 | `src/novelforge/stages/full_consistency_check.py` | 删除 |
| 12 | `src/novelforge/stages/generate_outline.py` | 删除 |
| 13 | `src/novelforge/stages/review_chapter.py` | 删除 |
| 14 | `src/novelforge/stages/review_characters.py` | 删除 |
| 15 | `src/novelforge/stages/review_outline.py` | 删除 |
| 16 | `src/novelforge/stages/review_simulation.py` | 删除 |
| 17 | `src/novelforge/stages/simulate_plot.py` | 删除 |
| 18 | `src/novelforge/stages/write_chapter.py` | 删除 |
| 19 | `src/novelforge/stages/_helpers.py` 中的 v3 shim 函数 | 删除（保留 `ensure_output_dirs` 等通用工具） |
| 20 | `config.py` 中的 `template` / `stages_override` / `scaffold_from` 字段 | 删除 |
| 21 | `orchestrator.py` 中的 v3 deprecation warning 代码 | 删除 |
| 22 | `templates.py` 中的 v3 `PIPELINE_TEMPLATES` 表 | 重构为 v4 默认 stage 配置（不再叫 template） |

### 4.4 配置 / 文档 / 样例

| # | 文件 | 改动 |
|---|---|---|
| 23 | `samples/minimal-novel/novel-project.yaml` | 重写为新 yaml 形态（produces + done_when） |
| 24 | `AGENTS.md` | 删除 v3 兼容章节；更新"Adding a New Pipeline Stage"为新模型 |
| 25 | `README.md` | pipeline 配置章节直接展示新 yaml 形态 |
| 26 | `docs/plan/pipeline-customization.md` | 更新或归档（不再描述 v3/v4 区别，只描述新模型） |
| 27 | `docs/plan/novelforge-design.md` | 更新为最新设计（删除 v3 残留示例） |

---

## 五、实施步骤（TDD）

按依赖顺序拆分，每步先写测试再写实现。每步独立 commit。

### Phase 1：基础设施（零依赖，可并行）

1. `errors.py` 加 `StageIncomplete`、`VerifyFailed`
2. `artifact_registry.py` + 单测（注册 / 取数 / 批量列表 / 持久化往返）
3. `verify.py` + 单测（6 种 kind 正反例 + callable 路径解析 + 占位符替换）

### Phase 2：契约模型（依赖 Phase 1）

4. `config.py` 重写 `StageConfig` + `ProduceSpec` + `CheckSpec` + `DoneWhenSpec`
5. 删除 v3 stage 类（10 个文件）+ `_helpers.py` shim 清理
6. 删除 `config.py` 的 `template` / `stages_override` / `scaffold_from`

### Phase 3：执行层（依赖 Phase 2）

7. `claude/adapter.py` 加 `completion_signal` + suffix 追加；Mock adapter 适配
8. `claude/context.py` 重写 `ContextAssembler`（依赖 ArtifactRegistry）
9. `stages/generic.py` 重构 `_run()`（produces 落盘 + registry 注册 + 两层协议）
10. `orchestrator.py` 改造 `_execute_with_retry`（新异常进整轮重跑）；删除 v3 兼容路径

### Phase 4：配置 / 样例 / 文档

11. `samples/minimal-novel/` 重写 yaml
12. `templates.py` 重构（默认 stage 配置）
13. `AGENTS.md` / `README.md` / `docs/plan/` 文档同步

---

## 六、测试计划

### 6.1 单元测试

| 模块 | 关键用例 |
|---|---|
| `test_artifact_registry.py` | 注册单文件；批量产物列表；持久化往返；alias 冲突报错 |
| `test_verify.py` | 6 种 kind 的正反例；callable 路径解析失败；target 占位符替换 |
| `test_config.py` | `produces` 必填校验；`done_when` 默认值；`split` 内联到 produces；yaml 解析多种组合 |
| `test_adapter.py` | suffix 默认追加；`completion_signal=None` 关闭；StageResult.completion_signal 赋值 |
| `test_context.py` | `{{upstream.X.Y}}` 占位符替换；批量产物列表展开；consumes 显式 vs 默认 |
| `test_generic.py` | 缺信号→StageIncomplete；done_when.checks 失败→VerifyFailed；produces 多文件落盘；registry 正确注册 |
| `test_orchestrator.py` | 整轮重跑计数；max_attempts 耗尽→on_failure；断点续传保留 attempts |

### 6.2 集成测试

在 `samples/minimal-novel/` 跑完整 pipeline，用 Mock adapter 模拟：

| 轮次 | Mock 行为 | 期望结果 |
|---|---|---|
| 第 1 轮 | stdout 缺 `<promise>COMPLETE</promise>` | 触发重跑（StageIncomplete） |
| 第 2 轮 | stdout 有完成信号，但 produces 文件为空 | 触发重跑（VerifyFailed） |
| 第 3 轮 | 通过所有 done_when.checks | stage 成功 |

并验证：
- `state.yaml` 的 `stage_attempts` 字段持久化正确
- `artifact_registry` 在 stage 间正确传递（用 `{{upstream.*}}` 占位符验证 prompt 实际接收到了上游内容）

### 6.3 质量门槛

- `pytest -q` 100% 通过
- `pytest --cov=novelforge --cov-report=term-missing` 覆盖率 ≥ 80%
- `novelforge validate samples/minimal-novel/` 退出码 0

---

## 七、风险与缓解

| 风险 | 缓解 |
|---|---|
| 死循环 | `max_attempts` 兜底，耗尽走 `on_failure`，与 `RouteCycleExceeded` 一致 |
| token 成本上升 | prompt 加 `attempt_hint` 告诉模型"上次没完成，这次务必..."，减少盲重试 |
| 上下文爆炸（默认 consumes 所有上游） | `ContextAssembler` 已有 token budget；超过预算时按"最近 stage 优先"截断并 warn |
| 产物路径写错（produces 与实际不符） | done_when 第一层就会失败（缺文件），不会静默通过；validate 阶段加静态检查 |
| prompt 污染（suffix 干扰模型） | `done_when.completion_signal: null` 可关闭 |
| Mock 模式测试 | `MockClaudeAdapter` 默认吐完成信号 + 写非空产物，既有测试适配即可 |
| 删除 v3 stage 类导致大量测试失败 | 测试也要同步重写为新模型；按 Phase 顺序逐步推进，每个 Phase 内测试全绿才进入下一个 |

---

## 八、未来扩展（不在本次范围）

- `done_when.checks.kind: rubric` — 调用 LLM 评分（与现有 review stage 重叠，先不引入）
- `done_when.checks.kind: shell` — 执行 shell 命令校验（安全考虑，先不引入）
- `consumes` 支持 glob（`consumes: ["generate_*"]`）
- `produces` 支持 `optional: true`（可选产物，缺失不阻塞）
- 跨 chapter 的 ArtifactRegistry 索引（让 review_chapter 能引用"全部已写章节"）

---

## 九、评审清单

- [ ] 三个核心需求（produces / done_when / 数据流）全部覆盖
- [ ] 与 cybervisor.yaml 的 `contract:` 设计对齐
- [ ] 错误档位矩阵清晰（A/B/C/D 四档互斥不重叠）
- [ ] 测试计划覆盖关键路径（缺信号、done_when 失败、整轮重跑、断点续传、数据流占位符）
- [ ] 死循环兜底（max_attempts + on_failure）
- [ ] v3 残留代码（10 个 stage 类 / template / stages_override / scaffold_from）**全部清理**，不留兼容
- [ ] 实施步骤可独立 commit、可回滚
