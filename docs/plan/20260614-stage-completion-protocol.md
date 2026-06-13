# Stage 完成性协议与产物校验 设计方案

> **日期**：2026-06-14
> **状态**：设计待评审（design draft）
> **范围**：NovelForge 引擎执行层（`src/novelforge/`），向后兼容 v4 yaml
> **关联**：[pipeline-customization.md](./pipeline-customization.md)、[novelforge-design.md](./novelforge-design.md)

## 一、背景与问题陈述

### 1.1 现状

NovelForge 的 stage 执行层目前用两道关拦模型输出：

1. **`ClaudeAdapter.invoke`**（`claude/adapter.py:258`）以 **CLI 退出码** 判定成功——退出码为 0 即视为本 stage 调用成功。
2. **`_execute_with_retry`**（`orchestrator.py:707`）只对**基础设施类异常**重试：

   ```python
   except (RateLimited, WriteFailure, ContextOverflow) as exc:
       # 指数退避重试
   ```

   其他异常（`SchemaInvalid`、`OutputParseError`、`FundamentIssue` 等）一律上抛走 `on_failure`。

### 1.2 缺陷：模型"未真正完成"这一档没有拦截

最常见、最隐蔽的一类错误被漏掉了：

| 失败场景 | 当前行为 | 应有行为 |
|---|---|---|
| Claude 写了空文件就返回（exit 0） | 视为成功，推进下一 stage | 重跑本 stage |
| Claude 让写 3 章，实际只写 1 章就停 | 视为成功 | 重跑本 stage |
| Claude 输出了 JSON 但 verdict 是"需要更多信息" | 视为成功 | 重跑本 stage |
| Claude 完全没写产物文件就退出 0 | 视为成功 | 重跑本 stage |

**根因**：判定 stage 是否成功的依据是"exit code"，但 Claude 的 exit code 既不反映"是否做完"也不反映"产物是否合规"。

**用户原话点中要害**：
> 保证每个 pipeline 都能正常返回，而不是上一个 pipeline 还没做完，就跳到下一个。

## 二、参考实现

`/Users/russell/workingspace/blink/src/blink/loop/runner/claude.py` 用了两条互补的机制把"完成判定"的权力从模型手里拿回编排层：

### 2.1 机制 A — 完成信号协议（`ClaudeRunner.run`）

在 prompt 末尾追加一段 `COMPLETION_SUFFIX`，要求模型完成全部任务后输出唯一标识：

```
<promise>COMPLETE</promise>
```

每跑一轮 Claude 检查 stdout 是否包含这个 token，没有就 `sleep(2)` 后**重新 spawn 整个 Claude 进程**（不是 retry 同一次调用），最多 `max_rounds` 轮。

**关键**：把"是否完成"的判定权从"看 exit code"换成"看模型自报的完成信号"。

### 2.2 机制 B — `verify_fn` 主动校验（`run_claude`）

`run_claude(prompt, cwd, max_retries, verify_fn, ...)` 每跑完一次 Claude：

1. 先看 exit code（基础设施错 → 重试）
2. 再调用 `verify_fn(cwd)` 检查产物（业务方定义的判定函数）
3. verify 通过才返回 `True`，否则下一轮重跑

**关键**：编排层有"产物是否真的生成"的最终决定权，不能光信模型的话。

### 2.3 两套机制的组合

| 机制 | 适用场景 | NovelForge 应用 |
|---|---|---|
| A 完成信号 | Claude Code 多步工具调用、模糊完成定义 | 所有 stage 默认开启 |
| B verify_fn | 有明确产物校验标准 | stage yaml 声明式指定 |

## 三、方案设计

### 3.1 整体流程

```
   StageConfig: {prompt, output, completion_signal?, verify?, max_attempts}
                                  │
                                  ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │  for attempt in 1..max_attempts:                                │
   │                                                                 │
   │  ┌───────────────────────────────────────────────────────────┐  │
   │  │ 1. render prompt                                          │  │
   │  │    + EXECUTION_SUFFIX  ("立即执行,不要只描述")             │  │
   │  │    + COMPLETION_SUFFIX (<promise>COMPLETE</promise>)       │  │
   │  │    + attempt_hint (第 N 轮,上次没完成)                     │  │
   │  │                                                           │  │
   │  │ 2. adapter.invoke()              ← 基础设施错仍走原重试    │  │
   │  │                                                           │  │
   │  │ 3. completion_signal 检测                                  │  │
   │  │    ├─ 缺信号  → raise StageIncomplete                     │  │
   │  │    └─ 有信号  ↓                                          │  │
   │  │                                                           │  │
   │  │ 4. _persist()  落盘 (复用现有 output_parser)              │  │
   │  │                                                           │  │
   │  │ 5. verify_fn(files, project_root)                          │  │
   │  │    ├─ 不通过 → raise VerifyFailed                         │  │
   │  │    └─ 通过   ↓                                          │  │
   │  │                                                           │  │
   │  │ 6. return StageExecutionResult ✓                          │  │
   │  └───────────────────────────────────────────────────────────┘  │
   │                                                                 │
   │  except (StageIncomplete, VerifyFailed):                        │
   │    sleep(backoff); continue  ← 关键:整轮重跑,不只是重跑 invoke │
   │                                                                 │
   │  return None  # 全部轮次耗尽                                     │
   └─────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
              max_attempts 耗尽 → on_failure (pause/skip/fail)
```

### 3.2 改造点清单

| # | 文件 | 改动 |
|---|---|---|
| 1 | `errors.py` | 新增 `StageIncomplete`、`VerifyFailed`（继承 `NovelForgeError`） |
| 2 | `verify.py`（新） | `VerifySpec` 数据类 + `run_verify(spec, files, project_root) -> bool` |
| 3 | `config.py` | `StageConfig` 增加 3 字段：`completion_signal`、`verify`、`max_attempts`（向后兼容默认值） |
| 4 | `claude/adapter.py` | `StageResult` 增加 `completion_signal: bool`；`invoke()` 自动追加 suffix（可通过参数关闭） |
| 5 | `stages/generic.py` | `_run()` 在 invoke 后加 completion + verify 两道检查，失败抛新异常 |
| 6 | `orchestrator.py` | `_execute_with_retry` 把新异常加入"整轮重跑"集合；`state.extra` 持久化 `attempts` 计数（断点续传） |
| 7 | `claude/adapter.py` Mock | `MockClaudeAdapter` 默认输出 `<promise>COMPLETE</promise>`，测试不被影响 |

### 3.3 `StageConfig` 新字段（yaml 形态）

```yaml
pipeline:
  stages:
    - id: write_chapter
      model: claude-opus-4-7
      prompt: prompts/write-chapter.md
      output: output/chapters/{{num}}.md
      # ── 新增字段（都可省略，有默认值） ─────────────────────────
      completion_signal: "<promise>COMPLETE</promise>"   # 默认值，null 可关
      max_attempts: 3                                     # 默认 3
      verify:                                             # 默认 null（不校验）
        - kind: exists          # 产物文件存在
        - kind: min_chars       # 字数下限
          target: "{{num}}.md"
          value: 2500
```

### 3.4 verify 内置检查器

| kind | 说明 | 必填字段 | 例子 |
|---|---|---|---|
| `exists` | 产物文件存在且非空 | （无） | 大纲文件必须存在 |
| `min_chars` | 字符数下限 | `target`、`value` | 章节 ≥ 2500 字 |
| `min_bytes` | 字节数下限 | `target`、`value` | 大纲 ≥ 5KB |
| `regex_match` | 内容匹配正则 | `target`、`pattern` | 必须出现 `## 第.*章` |
| `json_field` | JSON 字段值匹配 | `target`、`field`、`equals` | review verdict == "APPROVED" |
| `callable` | 业务自定义 Python 函数路径 | `module:func` | `myproj.checks:chapter_quality` |

`target` 字段支持 `{{num}}`、`{{batch}}` 等与 `StageConfig.output` 相同的占位符。

### 3.5 三档错误的最终矩阵

| 错误档 | 例子 | 处置 | 来源 |
|---|---|---|---|
| A. 基础设施错 | 429 / 5xx / CLI 超时 | 同 prompt 重试 N 次（指数退避） | 现有保留 |
| B. 模型格式错 | JSON 不符合 schema | 不重试，走 on_failure | 现有保留 |
| **C. 模型未完成** | **没写产物 / 写残 / 缺完成信号** | **整轮重跑，带 attempt_hint** | **本方案新增** |
| D. 模型语义错 | 内容质量差 / OOC | review gate 有限循环 | 现有保留 |

### 3.6 与现有机制的关系（不冲突，互补）

| 现有机制 | 与新方案的关系 |
|---|---|
| `_execute_with_retry` 对 `RateLimited/WriteFailure/ContextOverflow` 的重试 | **保留**。基础设施错仍用旧路径（瞬时，同 prompt 重试有意义） |
| `review_gate` + `max_review_iterations` | **保留**。verify 是"产物是否生成"的形式校验，review 是"内容是否合格"的语义校验，两层互补 |
| `on_failure` 三态（pause/skip/fail） | **复用**。max_attempts 耗尽后走原 on_failure |
| `OutputParseError` / `SchemaInvalid` | **保留**。这是"格式完全不对"那一档，不进 verify 流程 |
| `checkpoint` / `state.extra.review_iterations` | **扩展**。新增 `state.extra.stage_attempts[stage_id]`，断点续传不丢计数 |
| `RouteCycleExceeded` | **保留**。review↔writer 循环兜底依然有效 |

## 四、实施步骤（TDD）

按依赖顺序拆分，每步都先写测试：

1. **`errors.py` 加 2 个异常**（独立 commit，零依赖）
2. **`verify.py` + `test_verify.py`**（7 种 kind + callable 路径解析；这一步可独立通过测试）
3. **`config.py` 扩展 `StageConfig` + yaml 解析**；现有所有 stage_config 测试保持通过
4. **`claude/adapter.py`**：`StageResult.completion_signal` + 自动追加 suffix（可关闭）；`MockClaudeAdapter` 默认吐完成信号
5. **`stages/generic.py`**：`_run()` 加 completion + verify 两道关
6. **`orchestrator.py`**：`_execute_with_retry` 改造，新增异常进"整轮重跑"集合；`state.extra.stage_attempts` 持久化
7. **`samples/minimal-novel/novel-project.yaml`** 加示例 verify（让 sample 跑通就覆盖到了主要路径）
8. **`docs/agents/`（AGENTS.md 关联文档）** 更新"Adding a New Pipeline Stage"章节，加入新字段说明
9. **`README.md`** 在 pipeline 配置章节加入三个新字段的速查表

## 五、测试计划

### 5.1 单元测试

| 模块 | 关键用例 |
|---|---|
| `test_verify.py` | 每个 kind 的正例 + 反例；callable 路径解析失败；target 占位符替换 |
| `test_config.py` 扩展 | 新字段默认值正确；yaml 解析三种组合（仅 completion / 仅 verify / 全有）；v3 yaml 不报错 |
| `test_adapter.py` 扩展 | suffix 默认追加；`completion_signal=None` 时关闭；StageResult.completion_signal 正确赋值 |
| `test_generic.py` 扩展 | 缺信号→StageIncomplete；verify 失败→VerifyFailed；成功路径不动 |
| `test_orchestrator.py` 扩展 | 整轮重跑计数；max_attempts 耗尽→on_failure；断点续传保留 attempts |

### 5.2 集成测试

- 在 `samples/minimal-novel/` 跑完整 pipeline，用 Mock adapter 模拟：
  - 第 1 轮缺完成信号 → 触发重跑
  - 第 2 轮完成信号有但产物空 → 触发重跑
  - 第 3 轮通过 → stage 成功
- 验证 `state.yaml` 的 `stage_attempts` 字段持久化正确

### 5.3 回归

- 现有所有 `pytest -q` 测试必须 100% 通过
- `pytest --cov=novelforge --cov-report=term-missing` 覆盖率不低于现有阈值（80%）

## 六、风险与缓解

| 风险 | 缓解 |
|---|---|
| 死循环 | `max_attempts` 兜底，耗尽走 `on_failure`，与 `RouteCycleExceeded` 一致 |
| token 成本上升 | prompt 加 `attempt_hint` 告诉模型"上次没完成，这次务必..."，减少盲重试 |
| prompt 污染 | `completion_signal: null` 可关闭；老 v3 yaml 完全不受影响 |
| Mock 模式测试 | `MockClaudeAdapter` 默认吐完成信号，既有测试不需要改 |
| verify 表达式解析失败 | 解析阶段抛 `ConfigError`，在 `novelforge validate` 阶段就拦住，不让运行时崩溃 |

## 七、未来扩展（不在本次范围）

- `verify.kind: rubric` —— 调用 LLM 评分（需要再开一个 stage，与现有 review 重叠，先不引入）
- `completion_signal` 支持正则匹配（当前只支持字面 token）
- `attempts` 之间允许微调 prompt（当前是固定 prompt + hint，不改核心 prompt）

## 八、评审清单

- [ ] 方案与现有 v4 设计文档（pipeline-customization.md）不冲突
- [ ] 向后兼容：v3 / 不带新字段的 v4 yaml 行为不变
- [ ] 错误档位矩阵清晰（A/B/C/D 四档互斥不重叠）
- [ ] 测试计划覆盖关键路径（缺信号、verify 失败、整轮重跑、断点续传）
- [ ] 死循环兜底（max_attempts + on_failure）
