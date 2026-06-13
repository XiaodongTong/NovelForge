# NovelForge

**AI 无人值守长篇小说创作引擎** — 把写小说当作软件工程来做。

> 声明式契约配置 + 流水线调度 + 双层完成校验 + 断点恢复，让 Claude 从种子到成稿自动写完 300 章长篇。

## 核心模型：Stage 契约

每个 stage 在 `novel-project.yaml` 中声明其契约：

- **`produces`** — 产出文件清单（含 `alias` 供下游引用）
- **`done_when`** — 完成判定（信号 + 检查项 + 重试上限）
- **`consumes`** — 上游 stage 依赖（默认所有、显式空、显式清单三态）

数据通过 `ArtifactRegistry` 流转，下游用 `{{upstream.<id>.<alias>}}` 占位符读取上游产出。运行时是纯线性流水线（无 v3 路由跳转）。

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
novelforge init --template long-epic --dir <path>   # 在空目录生成 v4 yaml + prompts/ + seeds + 运行时目录
novelforge init --template long-epic --skeleton-only # 只生成 yaml + prompts/ + 运行时目录（跳过 seeds）
```

### 命令参数

| 命令 | 参数 | 说明 |
|------|------|------|
| `run` | `--config <yaml>` | **必填**，项目配置文件路径 |
| | `--max-chapters <N>` | 调试用：限制目标章节数 |
| | `--use-mock` | 使用 mock 适配器（不调用真实 API） |
| `resume` | `--config <yaml>` | **必填** |
| | `--force-stage <id>` | 跳过 recovery plan，从指定 stage 开始 |
| | `--use-mock` | 使用 mock 适配器 |
| `status` | `--config <yaml>` | **必填** |
| `validate` | `--config <yaml>` | **必填** |
| `init` | `--template <name>` | 模板名（`long-epic` / `short-story`） |
| | `--dir <path>` | 项目目录（不存在则自动创建） |
| | `--force` | 覆盖现有 yaml / 种子（prompts 总是按 `--force` 决定） |
| | `--skeleton-only` | 仅生成 yaml + prompts + 运行时目录；跳过 outline/ 与 CLAUDE.md |

## 项目结构

`novelforge init --template <name>` 跑完之后得到一个这样的目录（v4.1+）：

```
<project_dir>/
├── novel-project.yaml          # 契约配置（引擎读）
├── CLAUDE.md                   # 【用户填】写作约束 / 边界说明
├── outline/                    # 【用户填】故事种子
│   ├── premise.md              #   核心冲突 + 主角北极星
│   └── world.md                #   世界设定：势力 / 时代 / 调性
├── characters/                 # 【引擎填】每个角色一个 .md
├── chapters-outline/           # 【引擎填】章节大纲
│   └── outline.md              #   generate_outline 的产物
├── output/                     # 运行时产物
│   ├── chapters/               #   最终章节正文（write_chapter）
│   ├── review/                 #   review JSON + final-polish 报告
│   ├── summaries/              #   旧版兼容保留位（默认空）
│   └── meta/                   #   旧版兼容保留位（默认空）
└── prompts/                    # 每个 stage 的 prompt 模板
    └── *.md
```

`outline/` 与 `CLAUDE.md` 是用户要手动填的种子（`--skeleton-only` 模式下 init 会跳过它们，留空目录由用户补）；其它 `characters/` `chapters-outline/` `output/` 目录的 `.gitkeep` 占位由 `init` 自动建好。

## 快速开始：从零到一个可跑项目

```bash
# 1. 在空目录里初始化项目骨架（v4.1+ 会一并生成 outline/ + CLAUDE.md 占位种子）
mkdir my-novel && cd my-novel
novelforge init --template long-epic --dir .

# 2. 填写种子文件（参考 init 时生成的 CLAUDE.md）
$EDITOR outline/premise.md       # 核心冲突 / 主角北极星
$EDITOR outline/world.md         # 世界设定
$EDITOR CLAUDE.md                # 写作约束（init 已写入模板，可按需调整）

# 3. 校验配置
novelforge validate --config novel-project.yaml
# 预期输出: Config OK: N chapter(s)

# 4. 运行流水线（mock 模式无需 API key，可先看连通性）
novelforge run --config novel-project.yaml --use-mock
# 预期: 依次执行 generate_outline → design_characters → write_chapter
#        → review_chapter → final_polish
#        在 characters/、chapters-outline/、output/ 下产出对应文件

# 5. 查看进度 / 中断恢复
novelforge status  --config novel-project.yaml
novelforge resume  --config novel-project.yaml --use-mock
```

## 快速开始：跑通最小 sample

```bash
cd samples/minimal-novel

# 1. 校验配置
novelforge validate --config novel-project.yaml
# 预期输出: Config OK: 1 chapter(s)

# 2. 运行流水线（mock 模式，无需 API key）
novelforge run --config novel-project.yaml --use-mock
# 预期: 依次执行 generate_outline → design_characters → write_chapter
#        → review_chapter
#        在 chapters-outline/、characters/、output/ 下产出对应文件

# 3. 查看进度
novelforge status --config novel-project.yaml
# 预期: JSON 输出包含 current_stage, stage_attempts, token_usage

# 4. 中断恢复
novelforge resume --config novel-project.yaml --use-mock
# 预期: 从上次 checkpoint 继续
```

## 配置参考：`novel-project.yaml`（v4 契约）

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
  stages:
    - id: generate_outline
      model: claude-opus-4-7
      prompt: prompts/generate-outline.md
      produces:
        - path: chapters-outline/outline.md                 # v4.1+：单文件大纲落到专用目录
          alias: outline
      done_when:
        max_attempts: 3
        completion_signal: "<promise>COMPLETE</promise>"   # 可空（关闭第一层）
        mode: all                                          # all | any
        checks:
          - kind: min_chars
            target: chapters-outline/outline.md
            value: 500

    - id: design_characters
      model: claude-opus-4-7
      prompt: prompts/design-characters.md
      consumes: [generate_outline]
      produces:
        - path: characters/{{slug}}.md                      # v4.1+：split mode，每个角色一个文件
          alias: characters
          split: "^#\\s+(?P<slug>[A-Za-z0-9_-]+)\\s*$"
      done_when:
        max_attempts: 3
        checks:
          - kind: min_chars
            target: characters/{{slug}}.md                  # 每文件独立校验
            value: 300

    - id: write_chapter
      model: claude-opus-4-7
      prompt: prompts/write-chapter.md
      consumes: [generate_outline, design_characters]       # 显式清单
      produces:
        - path: output/chapters/{{num:03d}}.md
          alias: chapter
      batch: 3                                              # 批量驱动
      done_when:
        max_attempts: 3
        checks:
          - kind: min_chars
            target: output/chapters/{{num:03d}}.md
            value: 1000

    - id: review_chapter
      model: claude-sonnet-4-6
      prompt: prompts/review-chapter.md
      consumes: []                                          # 显式空：无上游
      produces:
        - path: output/review/chapter-review.json
          alias: review
      done_when:
        checks:
          - kind: json_field
            target: output/review/chapter-review.json
            field: passed

execution:
  batch_size:
    outline: 50
    chapter: 3
  max_review_iterations: 3
  review_model: "claude-sonnet-4-6"
  write_model: "claude-opus-4-7"
  context:
    total: 200000
    context_reserve: 60000
    output_reserve: 12000
  retry:
    max_retries: 3
    backoff: "exponential"
    max_wait: 300
```

### Stage 契约 schema 简表

| 字段 | 必填 | 默认 | 语义 |
|------|------|------|------|
| `id` | ✅ | — | pipeline 内唯一 |
| `model` | ✅ | — | Claude 模型 ID |
| `prompt` | ✅ | — | 提示词（文件路径或 inline 文本） |
| `produces` | ✅ | — | 产出文件清单（每个含 `path` + `alias`） |
| `produces[].split` | ❌ | — | 切分正则（与 `batch` 互斥） |
| `done_when` | ❌ | 默认值 | 完成判定（`completion_signal` + `checks` + `max_attempts`） |
| `consumes` | ❌ | `null` | 上游依赖（`null`=全部、`[]`=无、`[...]`=显式） |
| `batch` | ❌ | `1` | 单次 stage 跑 N 个 item；`{{num}}` 渲染 |
| `on_failure` | ❌ | `pause` | 重试耗尽时：`pause` / `skip` / `fail` |
| `enabled` | ❌ | `true` | false 时跳过 |

### `done_when.checks` 支持的 6 种 kind

| kind | 字段 | 说明 |
|------|------|------|
| `exists` | `target` | 文件存在 |
| `min_chars` | `target`、`value: int` | 字符长度 ≥ value |
| `min_bytes` | `target`、`value: int` | 字节长度 ≥ value |
| `regex_match` | `target`、`pattern: str` | 内容含正则匹配 |
| `json_field` | `target`、`field: str`、`value?` | JSON 字段存在/等值 |
| `callable` | `target`、`callable: "module:func"` | 自定义 Python 函数 |

### 双层完成校验（spec §AC-2/AC-3）

1. **第一层**：模型在响应末尾发出 `done_when.completion_signal`（默认 `<promise>COMPLETE</promise>`）；缺失 → `StageIncomplete`，触发整 stage 重试。
2. **第二层**：orchestrator 对每个 `produces` 文件跑 `done_when.checks`；任一失败 → `VerifyFailed`，同样整 stage 重试。

重试上限为 `done_when.max_attempts`（默认 3）。耗尽后按 `on_failure` 处置。

## 双层错误矩阵（spec §4.3）

| 档位 | 异常类型 | 触发场景 | 处置 |
|------|----------|----------|------|
| **A 档（基础设施）** | `RateLimited` / `WriteFailure` / `ContextOverflow` | 网络抖动、超时、429 | 内层 retry（指数退避），不递增 C 档计数 |
| **B 档（模型格式）** | `SchemaInvalid` / `OutputParseError` | 模型输出不符合声明的 `.json` 后缀；split 正则不匹配 | 不重试，直接走 `on_failure` |
| **C 档（模型未完成）** | `StageIncomplete` / `VerifyFailed` | 缺完成信号 / checks 失败 | 整 stage 重试（带 `attempt_hint`），最多 `max_attempts` |
| **D 档（语义）** | （外部 review gate） | 模型语义偏离 | 由专门 review stage 自检 |

## 环境变量

| 变量 | 说明 |
|------|------|
| `NOVELFORGE_MOCK` | 设为 `1`/`true`/`yes` 时使用 mock 适配器（等同 `--use-mock`） |
| `NOVELFORGE_QUIET` | 设为 `1`/`true`/`yes` 时禁用控制台日志输出 |
| `ANTHROPIC_API_KEY` | Anthropic API 密钥（未安装 Claude Code CLI 时必需） |
| `NOVELFORGE_MOCK_NO_SIGNAL` | mock 第一次调用缺完成信号（仅测试用） |
| `NOVELFORGE_MOCK_EMPTY` | mock 第一次调用写空 produces（仅测试用） |
| `NOVELFORGE_MOCK_ALWAYS_FAIL` | mock 每次调用同时缺信号 + 写空（仅测试用） |

## 退出码

| 退出码 | 含义 |
|--------|------|
| 0 | 正常完成 |
| 1 | 引擎运行时错误 |
| 2 | 配置校验失败 |
| 3 | 流水线暂停（需人工介入后 `resume`） |
| 130 | 用户中断（Ctrl+C） |

## 常见故障与排错

| 现象 | 可能原因 | 处置 |
|------|----------|------|
| `claude: command not found` | Claude Code CLI 未安装 | 安装 CLI 或用 `--use-mock` 绕过 |
| `ANTHROPIC_API_KEY is not set` | 未设置 API key 且未登录 CLI | 运行 `claude login` 或 `export ANTHROPIC_API_KEY=...` |
| `Config validation failed` | yaml 不符合契约 schema | 检查 stage 字段，运行 `novelforge validate` |
| `state.yaml paused: true` | 多次重试耗尽 | 查看 `paused_reason` 与 `state.extra.stage_attempts`；修复后 `resume` |
| `checkpoint corrupt: hash mismatch` | 进程被杀导致部分写入 | 删除损坏 checkpoint 或 `--force-stage` |
| `context overflow` | 上下文超出预算 | 引擎按 consumes 顺序逆序裁剪并 warn |

## 开发

```bash
# 运行测试
pytest -q

# 运行测试并查看覆盖率
pytest --cov=novelforge --cov-report=term-missing

# 运行单个测试文件
pytest tests/test_e2e_contract.py -v
```

## 文档

- [Stage 契约协议](docs/plan/stage-contract.md) — 契约模型的权威描述
- [设计文档](docs/plan/novelforge-design.md) — 架构愿景、模块、流水线详情
- [产品规范](.cybervisor/artifacts/spec.md) — 产品边界、验收标准
- [实施计划](.cybervisor/artifacts/plan.md) — 模块设计、里程碑
- [手工验证指南](.cybervisor/artifacts/verify.md) — 端到端验证步骤
