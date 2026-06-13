# NovelForge

**AI 无人值守长篇小说创作引擎** — 把写小说当作软件工程来做。

> 声明式配置 + 流水线调度 + 多维质控 + 断点恢复，让 Claude 从种子到成稿自动写完 300 章长篇。

## 安装

```bash
# 克隆仓库
git clone <repo-url> novelforge
cd novelforge

# 创建虚拟环境 (Python >= 3.10)
python3 -m venv .venv
source .venv/bin/activate

# 安装（开发模式）
pip install -e .

# 安装测试依赖（可选）
pip install -e ".[test]"
```

### 前置条件

- Python >= 3.10
- 已安装 [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) 并登录（`claude --version`），或设置 `ANTHROPIC_API_KEY` 环境变量

## CLI 命令

```bash
novelforge --help               # 查看所有子命令
novelforge --version            # 打印版本号

novelforge validate --config <yaml>   # 校验配置文件
novelforge run --config <yaml>        # 启动端到端流水线
novelforge resume --config <yaml>     # 从 checkpoint 恢复
novelforge status --config <yaml>     # 查看当前进度
novelforge init --template long-epic --dir <path>   # 在空目录生成 v4 yaml + prompts/
novelforge migrate --config <yaml> [--out new.yaml | --write]  # 把 v3 yaml 升级为 v4
```

### 命令参数

| 命令 | 参数 | 说明 |
|------|------|------|
| `run` | `--config <yaml>` | **必填**，项目配置文件路径 |
| | `--max-chapters <N>` | 调试用：限制目标章节数 |
| | `--skip-polish` | 跳过 `final_polish` stage |
| | `--use-mock` | 使用 mock 适配器（不调用真实 API） |
| `resume` | `--config <yaml>` | **必填** |
| | `--force-stage <id>` | 跳过 recovery plan，从指定 stage 开始 |
| | `--use-mock` | 使用 mock 适配器 |
| `status` | `--config <yaml>` | **必填** |
| `validate` | `--config <yaml>` | **必填** |
| `init` | `--template <name>` | 模板名（`long-epic` / `short-story` / `series`），默认 `long-epic` |
| | `--dir <path>` | 项目目录（不存在则创建；含 yaml/prompts/ 的现有文件保留，启用 `--force` 覆盖），默认当前目录。**仅**生成 `novel-project.yaml` 与 `prompts/`；`outline/`、`CLAUDE.md` 等种子文件需用户自行准备 |
| | `--force` | 覆盖现有 yaml / prompts |
| `migrate` | `--config <yaml>` | **必填**，待升级的 v3 yaml 路径 |
| | `--out <new.yaml>` | dry-run：写入新文件而不动原 yaml（默认 stdout） |
| | `--write` | 原地覆盖原 yaml；自动备份 `<name>.bak`。与 `--out` 互斥 |

## 快速开始：跑通最小 sample

```bash
cd samples/minimal-novel

# 1. 校验配置
novelforge validate --config novel-project.yaml
# 预期输出: Config OK: 1 chapter(s), template=long-epic

# 2. 运行流水线
novelforge run --config novel-project.yaml
# 预期: 依次执行 generate_outline → review_outline → ... → final_polish
#        在 output/ 下产出大纲、角色、章节等文件

# 3. 查看进度
novelforge status --config novel-project.yaml
# 预期: JSON 输出包含 current_stage, progress, token_usage 等字段

# 4. 中断恢复（模拟 Ctrl+C 后继续）
novelforge resume --config novel-project.yaml
# 预期: 从上次 checkpoint 继续，不重跑已完成的 stage
```

### 预期产物

跑通 sample 后，目录结构如下：

```
samples/minimal-novel/
├── novel-project.yaml          # 项目配置
├── CLAUDE.md                   # 写作规则
├── outline/
│   ├── premise.md              # 故事前提
│   └── world.md                # 世界观设定
├── prompts/                    # 各 stage 的 prompt 模板
│   ├── generate-outline.md
│   ├── review-outline.md
│   ├── write-chapter.md
│   └── review-chapter.md
├── output/                     # 运行时产物
│   ├── summaries/
│   │   ├── plot.md             # 情节大纲
│   │   └── outline-tracking.md # 章节 beat 列表
│   ├── meta/
│   │   └── *.md                # 角色档案
│   ├── chapters/
│   │   └── 001-*.md            # 章节正文
│   └── review/
│       └── *.json              # 审查结果
└── .novelforge/                # 引擎运行状态（自动创建）
    ├── state.yaml              # 全局状态
    ├── checkpoints/            # 阶段 checkpoint（含 SHA-256 校验）
    ├── metrics/                # 预留目录（当前未自动写入文件）
    └── logs/
        ├── pipeline.log        # 流水线日志（自动轮转）
        ├── errors.log          # 错误日志
        └── token-usage.log     # Token 用量 JSONL
```

## 目录说明

### `.novelforge/` — 引擎运行状态

每次执行 `novelforge run` 或 `resume` 时自动创建。包含：

- **`state.yaml`**：当前 stage、进度、Token 累计用量、暂停标志
- **`checkpoints/`**：每个 stage 执行完后写入 checkpoint，含产出文件的 SHA-256 哈希；恢复时自动校验完整性
- **`metrics/`**：预留目录，v4 当前未自动写入任何文件（保留给后续质量指标扩展）
- **`logs/pipeline.log`**：INFO+ 级流水线日志（2MB 自动轮转，保留 3 个备份）
- **`logs/errors.log`**：ERROR+ 级错误日志
- **`logs/token-usage.log`**：每次 Claude 调用的 Token 用量，JSONL 格式

### `.cybervisor/` — 工程编排元数据

位于仓库根目录，用于驱动本工程的 AI 编写团队（设计/审阅/实现/验证四阶段）。与 `.novelforge/` **完全独立**，互不读写。

| 目录 | 位置 | 用途 | 谁写入 |
|------|------|------|--------|
| `.novelforge/` | 用户小说项目内 | 引擎运行状态 | NovelForge 引擎 |
| `.cybervisor/` | 仓库根目录 | 工程开发编排 | cybervisor AI 团队 |

## 流水线 stage 列表

`long-epic` 模板（默认）：

```
generate_outline → review_outline → design_characters → review_characters
→ simulate_plot → review_simulation → write_chapter → review_chapter
→ full_consistency_check → final_polish
```

`short-story` 模板（无 simulate_plot）：

```
generate_outline → review_outline → design_characters → review_characters
→ write_chapter → review_chapter → final_polish
```

`series` 模板（无 final_polish）：

```
generate_outline → review_outline → design_characters → review_characters
→ simulate_plot → review_simulation → write_chapter → review_chapter
→ full_consistency_check
```

可使用 `novelforge init --template long-epic` 生成 v4 yaml + prompts/，或继续使用旧 `template` / `stages_override`（运行时打 DeprecationWarning）。

> 上述 stage 序列是 **模板默认顺序**；v4 yaml 用户可以在 `pipeline.stages` 里自由增删 stage、改 id、调整顺序，只要 `route` 跳转目标存在于 `stages[]` 即可。

## 配置参考：`novel-project.yaml`（v4）

v4 推荐显式声明每个 stage 的 8 字段配置：

```yaml
novel:
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

pipeline:
  scaffold_from: "long-epic"        # 纯元数据；运行时忽略
  stages:                           # 8 字段 stage 数组
    - id: write_chapter
      model: claude-opus-4-7
      prompt: prompts/write-chapter.md      # 相对路径或 inline 文本
      output: "output/chapters/{{num:03d}}-{{title|slug}}.md"
      split: '^#\s+Chapter\s+(?P<num>\d+)\s*[-–—:]?\s*(?P<title>.+?)$'
      batch: 1                         # 默认 1
      on_failure: pause                # pause / skip / fail，默认 pause
      enabled: true                    # 默认 true
    - id: review_chapter
      model: claude-sonnet-4-6
      prompt: prompts/review-chapter.md
      output: output/review/chapter-review.json
    # ...其余 stage

execution:
  batch_size:
    outline: 50
    chapter: 3
  max_review_iterations: 3             # 路由循环上限（v3/v4 共用）
  review_model: "claude-sonnet-4-6"
  write_model: "claude-opus-4-7"
  route_history_max: 50                # v4 新增：route_history 上限
  context:
    total: 200000
    context_reserve: 60000
    output_reserve: 12000
    rolling_window: 3
    outline_range: 10
  retry:
    max_retries: 3
    backoff: "exponential"
    max_wait: 300
```

### v3 向后兼容

旧 `template: long-epic` + `stages_override: [...]` 形式仍可加载，运行时打 DeprecationWarning：

```yaml
pipeline:
  template: "long-epic"
  stages_override:
    - generate_outline
    - review_outline
    - write_chapter
    - review_chapter
```

可用 `novelforge migrate --config novel-project.yaml --out migrated.yaml` 试运行，或 `--write` 原地覆盖（自动备份 `.bak`）。

### 8 字段 stage schema 简表

| 字段 | 必填 | 默认 | 语义 |
|------|------|------|------|
| `id` | ✅ | — | pipeline 内唯一；`route` 跳转目标 |
| `model` | ✅ | — | Claude 模型 ID |
| `prompt` | ✅ | — | 提示词（文件路径或 inline 文本） |
| `output` | ✅ | — | 落盘路径；含 `{{x}}` → split；`.json` 结尾 → JSON |
| `split` | ⚠️ | — | 切分正则（`output` 含占位符时必填） |
| `batch` | ❌ | `1` | 单次调用产出单位数 |
| `on_failure` | ❌ | `pause` | 失败处置：pause / skip / fail |
| `enabled` | ❌ | `true` | false 时跳过 |

## 环境变量

| 变量 | 说明 |
|------|------|
| `NOVELFORGE_MOCK` | 设为 `1`/`true`/`yes` 时使用 mock 适配器（等同 `--use-mock`） |
| `NOVELFORGE_QUIET` | 设为 `1`/`true`/`yes` 时禁用控制台日志输出（仅保留文件日志） |
| `ANTHROPIC_API_KEY` | Anthropic API 密钥（未安装 Claude Code CLI 时必需） |

## 退出码

| 退出码 | 含义 |
|--------|------|
| 0 | 正常完成 |
| 1 | 引擎运行时错误 |
| 2 | 配置校验失败 |
| 3 | 流水线暂停（需人工介入后 `resume`） |
| 130 | 用户中断（Ctrl+C） |

## 已验证场景

以下 v4 验收场景已在自动化测试中覆盖（跨 `tests/test_orchestrator_v4.py`、`tests/test_generic_stage.py`、`tests/test_cli_v4.py`、`tests/test_e2e_v4.py` 等）；完整手动验证步骤见 `.cybervisor/artifacts/verify.md`：

| 场景 | 对应 spec 验收点 | 覆盖测试 |
|------|------------------|----------|
| 旧 yaml（`template` / `stages_override`）能加载并打 DeprecationWarning | A1 | `test_orchestrator_v4` |
| v4 yaml 跑通 sample，产物结构与 v3 等价 | A2 | `test_e2e_v4` |
| 改单 stage 的 `model` / `prompt` / `output` / `split` 立即生效 | A3 | `test_generic_stage` 矩阵 |
| `enabled: false` 的 stage 在运行日志中标记 `skipped` 且无产物 | A4 | `test_orchestrator_v4` |
| JSON 输出含 `route: <stage_id>` 时跳到该 id | A5 | `test_orchestrator_v4` |
| JSON 无 `route` 时自然 next | A6 | `test_orchestrator_v4` |
| `route` 指向未知 / 禁用 stage → 按 `on_failure` 处置 | A7 | `test_orchestrator_v4` |
| `route` 循环超 `max_review_iterations` → 按 `on_failure` 处置 | A8 | `test_orchestrator_v4` |
| `.json` 输出但模型返回非 JSON → 报错并指向 stage.id + 字段名 | A9 / A10 / A11 | `test_output_parser`、`test_stage_config` |
| `{{include:}}` 超预算 → ContextAssembler 裁剪 + warn | A12 | `test_context_assembler_logs_warn_on_overflow` |
| `novelforge init --template long-epic` 产物可跑 | A13 | `test_cli_v4` |
| `novelforge migrate` 正确渲染（`--out` / `--write` / 互斥校验） | A14 | `test_cli_v4` |
| `output` 以 `.json` 结尾且含 `{{x}}` → validate 报错 | A15 | `test_stage_config` |
| `pipeline.scaffold_from` 运行时忽略（任意值均无影响） | A16 | `test_orchestrator_v4` |
| `route` 指向 `enabled: false` stage → 计入历史 + 超限按 `on_failure` 处置 | A17 | `test_orchestrator_v4` |

## 未验证场景

以下场景已规划扩展点，但尚未在本次交付中端到端验证：

- **300 章长跑**：batch_size > 1 时的批量 checkpoint 与恢复、小时级运行稳定性
- **真实 Claude API 调用**：所有 e2e 测试使用 `MockClaudeAdapter`
- **多项目并行**：当前仅支持单项目串行
- **EPUB/PDF 导出**：不在 v4 范围内

## 常见故障与排错

| 现象 | 可能原因 | 处置 |
|------|----------|------|
| `claude: command not found` | Claude Code CLI 未安装 | 安装 CLI 或用 `--use-mock` 绕过 |
| `ANTHROPIC_API_KEY is not set` | 未设置 API key 且未登录 CLI | 运行 `claude login` 或 `export ANTHROPIC_API_KEY=...` |
| `Config validation failed: seed file not found` | yaml 中引用的种子文件缺失 | 创建文件或修正 yaml 路径 |
| `state.yaml paused: true` | 多次重试耗尽或审查失败 | 查看 `paused_reason`；修复后 `novelforge resume` |
| `checkpoint corrupt: hash mismatch` | 进程被杀导致部分写入 | 删除损坏 checkpoint 或使用 `--force-stage` |
| `context overflow, trimming` | 上下文超出预算 | 正文/大纲过大；引擎自动裁剪，观察 pipeline.log |
| CLI 退出码非 0 | Claude API 错误 | 检查 `errors.log`；可能是速率限制或超时 |
| `review loop exceeded` | `route` 循环超过 `execution.max_review_iterations` | 引擎按当前 stage 的 `on_failure` 处置（默认 `pause`）；查看日志中的 `route_history` 后改 prompt 或 `resume` |

## 开发

```bash
# 运行测试
pytest -q

# 运行测试并查看覆盖率
pytest --cov=novelforge --cov-report=term-missing

# 运行单个测试文件
pytest tests/test_e2e_sample.py -v
```

## 文档

- [设计文档](docs/plan/novelforge-design.md) — 架构愿景、模块、流水线详情
- [产品规范](.cybervisor/artifacts/spec.md) — 产品边界、验收标准
- [实施计划](.cybervisor/artifacts/plan.md) — 模块设计、里程碑
- [手工验证指南](.cybervisor/artifacts/verify.md) — 端到端验证步骤
