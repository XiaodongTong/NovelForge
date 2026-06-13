# Write Chapter — prompt

You are writing one chapter of a long-form webnovel.  The orchestrator
has attached the upstream outline via the
`{{upstream.generate_outline.outline}}` placeholder; read it for the
chapter beat you must hit.

Output a single chapter in markdown.  The chapter must:

- Open with `## Chapter 1 - <Title>` so the engine can detect it.
- Contain 400–1500 Chinese characters of prose.
- End on a small reversal as required by the project's CLAUDE.md.

Do not output anything other than the chapter itself.
