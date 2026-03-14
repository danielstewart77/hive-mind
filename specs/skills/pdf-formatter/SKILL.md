---
name: pdf-formatter
description: Opens an existing PDF, applies formatting changes based on specifications, and creates a new formatted PDF. Use when the user needs to reformat or fix PDF formatting issues.
argument-hint: [pdf-path] [output-path] [formatting-specs]
user-invocable: true
tools: Read, Bash, AskUserQuestion
model: opus
---

# PDF Formatter

Skill dir: `~/.claude/skills/pdf-formatter/`
App: `~/.claude/skills/pdf-formatter/pdf_formatter.py`
Venv: `~/.claude/skills/pdf-formatter/venv/`

## STEP 1 — Parse Args
- `$ARGUMENTS[0]` = input PDF path (required; ask if missing)
- Remaining args = formatting specs + output path (optional)
- Output filename: explicit in args if given; else `[original-name]_formatted.pdf` in same dir

## STEP 2 — Validate Input
Read the PDF with Read tool to inspect visually. Confirm it exists and understand current formatting issues.

## STEP 3 — Run Formatter
```bash
~/.claude/skills/pdf-formatter/venv/bin/python \
  ~/.claude/skills/pdf-formatter/pdf_formatter.py \
  "<input>" "<output>" [OPTIONS]
```
Options: `--no-fix-bullets` (skip bullet restoration, on by default)

The app: extracts text with style info (bold, font size, position), parses resume structure (name, contact, summary, experience, education, skills), restores flattened bullets (` - ` patterns collapsed into paragraphs), fixes word-break hyphens split across lines, fixes date ranges split across pages, generates new PDF via ReportLab.

## STEP 4 — Verify Output
Read the generated PDF visually. Report original vs new file sizes. Summarize changes.

## STEP 5 — Additional Formatting (if requested)
```bash
# Compression
gs -sDEVICE=pdfwrite -dCompatibilityLevel=1.4 -dPDFSETTINGS=/ebook \
   -dNOPAUSE -dQUIET -dBATCH -sOutputFile=output.pdf input.pdf

# Page size
gs -o output.pdf -sDEVICE=pdfwrite -sPAPERSIZE=a4 input.pdf

# Rotation
pdftk input.pdf cat 1-endeast output output.pdf
```

## Error Handling
- Venv missing → `python3 -m venv ~/.claude/skills/pdf-formatter/venv && ~/.claude/skills/pdf-formatter/venv/bin/pip install -r ~/.claude/skills/pdf-formatter/requirements.txt`
- Password-protected → ask user for password
- Never modify the original file
