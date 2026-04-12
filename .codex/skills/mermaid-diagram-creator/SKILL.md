---
name: mermaid-diagram-creator
description: Creates and validates Mermaid diagrams with syntax checking. Use when users need to create or fix Mermaid diagrams that won't render.
argument-hint: [diagram-type]
---

# Mermaid Diagram Creator

Skill dir: `~/.claude/skills/mermaid-diagram-creator/`
Primary editor: Obsidian — diagrams valid in `mmdc` render correctly in Obsidian.

## STEP 1 — Parse Args
`$ARGUMENTS[0]` = diagram type: `sequence|flowchart|gantt|class|state|er|journey|pie`. Ask if missing.

## STEP 2 — Gather Requirements
Ask user: entities/flow/relationships, labels, content.

## STEP 3 — Create Diagram

**All types:**
- No YAML frontmatter (`--- title: ... ---`)
- No `title` directive in body — use a markdown heading above the block
- 4-space indentation

**Sequence diagrams:**
- `participant Alias as Display Name` (multi-char aliases only, e.g. `Tts`, `Bot`, never single letters)
- `->>` solid+arrowhead, `-->>` dotted+arrowhead, `->` solid, `-->` dotted
- No blank lines between participant declarations and messages
- Avoid `end` in message text; wrap in quotes if unavoidable
- Self-reference valid: `HM->>HM: Process`

**Flowchart:**
- `graph TD` or `graph LR`
- `A[Label]`, `B(Rounded)`, `C{Diamond}`, `D((Circle))`

## STEP 4 — Write to Temp File
```bash
cat > ~/.claude/skills/mermaid-diagram-creator/diagram.mmd <<'EOF'
[diagram content — no ```mermaid fences]
EOF
```

## STEP 5 — Validate
```bash
mmdc -p ~/.claude/skills/mermaid-diagram-creator/puppeteer-config.json \
     -i ~/.claude/skills/mermaid-diagram-creator/diagram.mmd \
     -o ~/.claude/skills/mermaid-diagram-creator/diagram.svg 2>&1
```
- "Generating single mermaid chart" + no errors → valid
- Errors → fix syntax, retry STEP 5

## STEP 6 — Output
Provide validated diagram as a ` ```mermaid ``` ` code block. Use Edit/Write to insert into file if requested.

## Troubleshooting
| Issue | Fix |
|-------|-----|
| `actor` syntax error | Use `participant` |
| `end` breaks diagram | Wrap: `"message with end"` |
| mmdc sandbox error | Already handled by `-p puppeteer-config.json` |
| VS Code plain text | Install `bierner.markdown-mermaid` |
| MarkText position 3+ fails | MarkText bug — place mermaid blocks before other code blocks or use Obsidian |
