# Writing rules

These are the writing rules the engine must follow at all times.

1. Stay in third-person limited perspective unless the prompt explicitly
   allows an alternative.
2. Keep every chapter between `novel.words_per_chapter[0]` and
   `novel.words_per_chapter[1]` Chinese characters; pad with sensory
   detail, never filler.
3. Never break the rule "no resurrection without cost".  Miracles come
   with named prices.
4. Avoid modern slang, brand names, and direct internet references.
5. End every chapter on a small reversal — a question, a betrayal, a
   quiet cost — that compels the reader to continue.
6. The protagonist's voice should hint at suppressed grief; never
   overwrite it with on-the-nose exposition.

## Project directory boundary (v4.1)

```
samples/minimal-novel/
├── novel-project.yaml          # 契约配置（引擎读）
├── CLAUDE.md                   # 本文件：写作约束
├── outline/                    # 【用户填】种子
│   ├── premise.md
│   └── world.md
├── prompts/                    # 每个 stage 的 prompt 模板
├── characters/                 # 【引擎填】split-mode，slug=[A-Za-z0-9_-]+
├── chapters-outline/outline.md # 【引擎填】generate_outline 产物
└── output/
    ├── chapters/               #   write_chapter 批次
    └── review/                 #   review JSON + final-polish 报告
```

The engine only writes under `characters/`, `chapters-outline/`,
`output/`. Everything under `outline/` and this `CLAUDE.md` is the
user's; the engine will never touch them.