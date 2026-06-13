# NovelForge — AI 无人值守小说创作引擎 设计方案

> **状态**：本文件是早期 v3 阶段的架构愿景与模块设计。当前运行时模型已升级到 **v4**（统一 `pipeline.stages` 8 字段 + `GenericStage`）；用户/贡献者编写 `novel-project.yaml` 与阅读 stage schema 时，请以 [`pipeline-customization.md`](./pipeline-customization.md) 为准。
>
> 本文件保留作为架构愿景与模块布局参考；其中的 yaml / stage / review 示例属 v3 形态，**不应**直接复制到 v4 配置里。

## 一、系统定位

一个**声明式、可配置、无人值守**的 AI 长篇小说创作引擎。用户只需提供故事种子（前提、世界观、风格要求），引擎自动完成从大纲到成稿的全流程，产出 300 章、80 万字级别的完整长篇小说。

核心设计哲学：**把写小说当作软件工程来做** — 需求分析 → 架构设计 → 详细设计 → 编码实现 → 代码审查 → 回归测试，每一步都有质量门禁。

## 二、架构总览

```
┌─────────────────────────────────────────────────────┐
│                   NovelForge Engine                   │
├──────────┬──────────┬──────────┬──────────┬─────────┤
│ 配置层    │ 调度层    │ 执行层    │ 质控层    │ 持久层  │
│          │          │          │          │         │
│ project  │ pipeline │ claude   │ review   │ state   │
│ .yaml    │ orchestr │ adapter  │ gate     │ store   │
│          │ ator     │          │          │         │
├──────────┴──────────┴──────────┴──────────┴─────────┤
│                  Claude API / Claude Code CLI          │
└─────────────────────────────────────────────────────┘
```

## 三、核心模块设计

### 3.1 配置层 — `novel-project.yaml`

声明式项目配置，一个 YAML 文件定义整部小说：

```yaml
# novel-project.yaml — 项目声明文件

novel:
  title: "众生之道"
  genre: "玄幻修仙"
  target_chapters: 300
  words_per_chapter: [2500, 3000]  # min-max
  style: "天蚕土豆、辰东"

  # 故事种子（必填，人工提供）
  seeds:
    - outline/premise.md    # 故事前提与核心冲突
    - outline/world.md      # 世界观设定

  # 写作风格约束（必填）
  constraints:
    - CLAUDE.md             # 所有写作规则和禁忌

# 流水线模板（可选，有默认值）
pipeline:
  template: "long-epic"     # 内置模板：long-epic / short-story / series

  # 也可以完全自定义 stages（高级用户）
  stages_override: null

# 执行参数
execution:
  batch_size:
    outline: 50             # 大纲每批生成章节数
    chapter: 3              # 正文每次写几章
  max_review_iterations: 3  # 每个审查阶段最大循环次数
  review_model: "claude-sonnet-4-6"    # 审查用的模型
  write_model: "claude-opus-4-7"       # 写作用的模型
```

### 3.2 调度层 — Pipeline Orchestrator

无人值守的**核心**。一个有限状态机 (FSM)，驱动各阶段按条件流转。

```
┌──────────┐  contract   ┌──────────┐  contract   ┌──────────┐
│  Stage A  │───────────▶│  Stage B  │───────────▶│  Stage C  │
│ (Execute) │  routing    │ (Execute) │  routing    │ (Execute) │
└────┬─────┘             └────┬─────┘             └──────────┘
     │                        │
     │ fail                   │ retry
     ▼                        ▼
┌──────────┐             ┌──────────┐
│  Error   │             │  Back to │
│ Recovery │             │  Stage A │
└──────────┘             └──────────┘
```

**Stage 定义规范：**

```yaml
stages:
  - id: generate_outline
    name: "生成大纲"
    role: "资深网文策划编辑"
    prompt_file: "prompts/generate-outline.md"   # prompt 模板文件
    contract:
      inputs:                          # 本阶段需要的上下文
        - seeds/**                     # 故事种子
        - constraints/**               # 写作约束
        - state/outline-progress.yaml  # 进度状态
      outputs:                         # 本阶段产出的文件
        - output/summaries/plot.md
        - output/summaries/outline-tracking.md
      routes:                          # 路由决策
        - id: BATCH_DONE
          condition: "未写满 target_chapters"
          next: generate_outline       # 自循环
        - id: COMPLETE
          condition: "已写满 target_chapters"
          next: review_outline
    retry:
      max_retries: 3
      backoff: exponential
```

**调度器核心逻辑（伪代码）：**

```python
class PipelineOrchestrator:
    def run(self):
        state = StateStore.load()
        current_stage = state.current_stage

        while not state.is_complete:
            # 1. 组装上下文 — 按当前 stage 的 inputs 声明加载文件
            context = self.assemble_context(current_stage)

            # 2. 调用 Claude — 用 prompt 模板 + 上下文
            result = self.execute_stage(current_stage, context)

            # 3. 解析 contract — 模型输出路由决策
            route = self.parse_contract(result, current_stage)

            # 4. 持久化状态 — 断点恢复的关键
            state.checkpoint(current_stage, route)

            # 5. 状态流转
            current_stage = route.next
            state.current_stage = current_stage
```

### 3.3 执行层 — Claude Adapter

负责与 Claude 交互，管理上下文窗口和 token 预算。

```yaml
# 执行层的关键设计

context_strategy:
  # 滚动上下文 — 写第 N 章时只加载：
  # - 全局设定（premise, world）— 始终在上下文
  # - 大纲中当前 ±10 章范围
  # - 最近 3 章正文（衔接参考）
  # - 人物小传（本批次出场人物）
  # - 伏笔追踪表（全文，因为它很短）

  always_loaded:              # 常驻上下文（约 3000-5000 tokens）
    - outline/premise.md
    - outline/world.md
    - CLAUDE.md

  rolling_window: 3           # 滚动加载最近 N 章正文
  outline_range: 10           # 大纲加载前后 N 章范围

  budget:                     # token 预算分配
    total: 200000             # Claude 上下文窗口大小
    context_reserve: 60000    # 给输入上下文预留
    output_reserve: 12000     # 给输出预留（每章 ~3000 字 ≈ 4000 tokens × 3章）
```

### 3.4 质控层 — Review Gate

多维度审查，每个维度有明确的通过/不通过标准：

```yaml
review_dimensions:
  outline_review:
    - dimension: 主题一致性
      criteria: "大纲是否贯穿核心立意"
      severity: critical        # critical = 必须通过

    - dimension: 节奏把控
      criteria: "高潮分布是否合理，每 5-8 章小高潮，25-30 章大转折"
      severity: major

    - dimension: 伏笔网络
      criteria: "暗线有埋有收，时机得当"
      severity: major

  chapter_review:
    - dimension: 大纲符合度
      criteria: "核心事件按大纲推进"
      severity: critical

    - dimension: 角色一致性
      criteria: "对话和行为符合角色档案，无 OOC"
      severity: critical

    - dimension: 字数达标
      criteria: "每章 ≥ 2500 中文字符"
      severity: critical
      auto_check: true         # 可机器验证的维度

    - dimension: 文风质量
      criteria: "符合目标风格（天蚕土豆/辰东）"
      severity: major

  # 审查结果的结构化输出
  review_output_schema:
    passed: boolean
    findings: list             # 具体问题列表
    required_changes: list     # 必须修改的内容
    route: enum                # APPROVED / NEEDS_REWRITE / FUNDAMENTAL_ISSUE
```

### 3.5 持久层 — State Store

**无人值守能否成功的关键**。所有状态持久化到磁盘，支持随时断点恢复。

```yaml
# 状态目录结构
.novelforge/
├── state.yaml              # 全局状态：当前 stage、总体进度
├── checkpoints/            # 每个 stage 完成后的快照
│   ├── outline-done.yaml
│   ├── characters-done.yaml
│   ├── chapter-001-003.yaml
│   ├── chapter-004-006.yaml
│   └── ...
├── logs/
│   ├── pipeline.log        # 流水线执行日志
│   ├── token-usage.log     # Token 用量追踪
│   └── errors.log          # 错误日志
└── metrics/                # 预留：质量指标（阶段一未自动写入）
```

```yaml
# state.yaml 示例
current_stage: write_chapter
pipeline_version: "1.0"
started_at: "2026-06-06T10:00:00Z"
last_checkpoint: "2026-06-07T03:22:00Z"

progress:
  outline: complete           # 300/300
  characters: complete        # 20/20
  simulation: complete
  chapters_written: 147       # 当前写到第 147 章
  chapters_reviewed: 144      # 已审查到第 144 章
  total_words: 485000

recovery:
  last_batch_chapters: [145, 146, 147]
  last_batch_status: written_not_reviewed
  # 恢复时：从 last_batch_status 对应的 stage 继续执行
```

## 四、完整流水线设计

### Phase 1: PRE-PRODUCTION（预制作）

```
┌──────────────┐     ┌──────────────┐
│ 1. Generate   │────▶│ 2. Review    │
│    Outline    │◀────│    Outline   │
│  (50章/批×6)  │修改 │              │
└──────────────┘     └──────┬───────┘
                              │ 通过
┌──────────────┐     ┌──────▼───────┐
│ 4. Review    │◀────│ 3. Design    │
│    Characters│─────│    Characters│
└──────┬───────┘修改 └──────────────┘
       │ 通过
┌──────▼───────┐     ┌──────────────┐
│ 6. Review    │◀────│ 5. Simulate  │
│    Simulation│─────│    Plot      │
└──────┬───────┘修改 └──────────────┘
       │ 通过
```

### Phase 2: PRODUCTION（制作）— 主体循环

```
┌──────────────────────────────────────┐
│                                      │
│  ┌───────────┐    ┌──────────────┐  │
│  │ 7. Write   │───▶│ 8. Review    │  │
│  │  Chapter   │◀───│    Chapter   │  │
│  │ (3章/批)   │重写│              │  │
│  └───────────┘    └──────┬───────┘  │
│                          │通过       │
│         未满300章 ────────┘          │
│                          │已满       │
└──────────────────────────┼──────────┘
                           │
```

### Phase 3: POST-PRODUCTION（后制作）

```
┌──────▼───────┐     ┌──────────────┐
│ 9. Full      │────▶│ 10. Final    │
│ Consistency  │     │    Polish    │
│ Check        │     │              │
└──────────────┘     └──────┬───────┘
                              │
                       ┌──────▼───────┐
                       │ ✅ 成稿输出   │
                       │  EPUB/PDF    │
                       └──────────────┘
```

### 流水线总览

| 阶段 | Stage | 输入 | 输出 | 循环 |
|------|-------|------|------|------|
| 预制作 | Generate Outline | 种子 + 进度 | plot.md + tracking | 每批50章，共6批 |
| 预制作 | Review Outline | plot.md + 种子 | 修改后 plot.md | 最多3轮 |
| 预制作 | Design Characters | plot.md + world | 角色档案 | 按角色分批 |
| 预制作 | Review Characters | 角色档案 | 修改后档案 | 最多3轮 |
| 预制作 | Simulate Plot | plot.md + 角色 | plot-simulation.md | 每个关键节点 |
| 预制作 | Review Simulation | simulation + plot | 审查意见 | 最多2轮 |
| 制作 | Write Chapter | 全部素材 | 3章正文 | 每批3章，共100批 |
| 制作 | Review Chapter | 正文 + 素材 | 审查 + 修正 | 每批1次 |
| 后制作 | Consistency Check | 全部正文 | 一致性报告 | 1次 |
| 后制作 | Final Polish | 全部正文 | 最终成稿 | 1次 |

## 五、无人值守的关键技术

### 5.1 断点恢复

```python
# 每个批次完成后的 checkpoint 流程
def checkpoint(stage, batch_info):
    state = {
        "stage": stage,
        "batch": batch_info,
        "timestamp": now(),
        "files_snapshot": hash_all_output_files(),  # 文件完整性校验
    }
    save(f".novelforge/checkpoints/{stage}-{batch_info.id}.yaml", state)
```

恢复逻辑：

1. 读取 `state.yaml`，确定 `current_stage`
2. 读取对应 checkpoint，验证文件完整性
3. 从断点 stage 继续执行
4. 人工干预最少化：唯一需要人工确认的是 checkpoint 文件损坏的情况

### 5.2 Token 预算管理

```
单次写作批次（3章）的 Token 分配策略：

┌─────────────────────────────────────────┐
│  输入上下文 (~50,000 tokens)             │
│  ├─ 常驻设定 (premise + world + rules)  │  ~5,000
│  ├─ 大纲窗口 (当前 ±10 章)              │  ~3,000
│  ├─ 人物小传 (本批次出场)                │  ~3,000
│  ├─ 伏笔追踪表                          │  ~2,000
│  ├─ 前 3 章正文（衔接）                  │  ~15,000
│  └─ prompt 模板 + 指令                   │  ~2,000
├─────────────────────────────────────────┤
│  输出 (3 章 × ~4,000 tokens/章)          │  ~12,000
├─────────────────────────────────────────┤
│  余量                                    │  ~138,000
└─────────────────────────────────────────┘
```

### 5.3 错误恢复策略

```yaml
error_handling:
  write_failure:
    strategy: retry_with_fresh_context
    max_retries: 3
    on_exhausted: pause_and_notify  # 暂停，等待人工介入

  review_loop_exceeded:             # 审查循环超过 max_iterations
    strategy: escalate
    action: "降级处理 — 接受当前版本，标记为待人工复核"

  context_window_overflow:          # 上下文溢出
    strategy: trim_context
    rules:
      - 先裁剪历史正文（只保留最近 1 章结尾）
      - 再裁剪大纲窗口（±10 → ±5）
      - 最后裁剪人物小传（只保留主要角色）

  api_rate_limit:
    strategy: exponential_backoff
    max_wait: 300  # 最长等 5 分钟
```

## 六、与现有 cybervisor 的演进关系

| 维度 | 现有 cybervisor.yaml | NovelForge |
|------|---------------------|------------|
| 配置 | 单一 YAML，硬编码本小说 | 项目声明 + 流水线模板，可复用 |
| 调度 | cybervisor 工具驱动 | 内置 FSM 调度器 |
| 断点恢复 | 依赖 cybervisor 的 checkpoint | 独立 state store + 文件校验 |
| 上下文管理 | 人工通过 prompt 描述 | 自动 token 预算分配 |
| 质控 | prompt 描述审查维度 | 结构化 schema + 自动检查 |
| 可复用性 | 需手动改 YAML | 换个 project.yaml 就能写新小说 |

## 七、实现路径

### 阶段一：脚本编排器（1-2 周）

- Python/Node 脚本，读取 `novel-project.yaml`
- 通过 Claude Code CLI 的 `--prompt` 模式调用
- 实现基本 FSM 调度和 checkpoint
- 用现有 300 章项目作为测试用例验证

### 阶段二：质控强化（1 周）

- 结构化审查输出（JSON schema）
- 自动检查项（字数、文件命名、伏笔一致性）
- 审查评分系统和质量趋势追踪

### 阶段三：平台化（可选）

- Web UI 查看进度和质量报告
- 多项目并行写作
- 一键导出 EPUB/PDF

## 八、项目目录结构

```
novel-project/
├── novel-project.yaml          # 项目声明（引擎读取）
├── CLAUDE.md                   # 写作规则和禁忌
├── outline/                    # 故事种子（人工提供）
│   ├── premise.md
│   └── world.md
├── prompts/                    # prompt 模板（可自定义覆盖默认）
│   ├── generate-outline.md
│   ├── review-outline.md
│   ├── design-characters.md
│   ├── review-characters.md
│   ├── simulate-plot.md
│   ├── review-simulation.md
│   ├── write-chapter.md
│   └── review-chapter.md
├── output/                     # 引擎产出
│   ├── summaries/              # 大纲、推演、进度
│   ├── chapters/               # 正文章节
│   ├── review/                 # 审查报告
│   └── meta/                   # 角色档案、伏笔追踪
├── .novelforge/                # 引擎状态（自动生成）
│   ├── state.yaml
│   ├── checkpoints/
│   ├── logs/
│   └── metrics/
└── docs/
    └── plan/                   # 设计文档
```
