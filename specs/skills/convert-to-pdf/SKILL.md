---
name: convert-to-pdf
description: Converts documents to PDF using a Python script in a .venv environment. Currently supports .md (Markdown) files. Use when you need to convert a document to PDF.
argument-hint: "[input-file] [output-file]"
user-invocable: true
tools: Bash, Read, Write, Glob
---

# Convert to PDF

### Step 1: Parse Arguments

- `$ARG0` = Input file path (required, e.g., `README.md`)
- `$ARG1` = Output PDF path (optional, defaults to input filename with `.pdf` extension)

If no input file is provided, ask the user for it.

Resolve paths relative to the current working directory.

### Step 2: Validate Input

Check that the input file exists and has a supported extension.

Supported: `.md`

If the file doesn't exist or the extension is unsupported, report the error clearly and stop.

### Step 3: Set Up .venv and Script

The skill directory is `~/.claude/skills/convert-to-pdf/`. All tooling lives there — never in the user's working directory.

Set `SKILL_DIR=~/.claude/skills/convert-to-pdf`.

Check if `$SKILL_DIR/.venv` exists. If not, create it and install packages:

```bash
python3 -m venv "$SKILL_DIR/.venv" && "$SKILL_DIR/.venv/bin/pip" install markdown weasyprint --quiet
```

If `.venv` already exists, skip creation.

Write the conversion script to `$SKILL_DIR/convert_to_pdf.py` if it doesn't already exist:

```python
#!/usr/bin/env python3
"""Convert documents to PDF."""

import sys
import pathlib

def md_to_pdf(input_path: str, output_path: str) -> None:
    import markdown
    from weasyprint import HTML

    source = pathlib.Path(input_path).read_text(encoding="utf-8")
    html_body = markdown.markdown(source, extensions=["tables", "fenced_code"])
    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: Georgia, serif; max-width: 800px; margin: 40px auto; padding: 0 20px; line-height: 1.6; color: #222; }}
  h1, h2, h3, h4, h5, h6 {{ font-family: Arial, sans-serif; color: #111; margin-top: 1.4em; }}
  code {{ background: #f4f4f4; padding: 2px 5px; border-radius: 3px; font-size: 0.9em; }}
  pre {{ background: #f4f4f4; padding: 12px; border-radius: 4px; overflow-x: auto; }}
  pre code {{ background: none; padding: 0; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #ccc; padding: 6px 12px; }}
  th {{ background: #f0f0f0; }}
  blockquote {{ border-left: 4px solid #ccc; margin: 0; padding-left: 16px; color: #555; }}
</style>
</head>
<body>
{html_body}
</body>
</html>"""
    HTML(string=html, base_url=str(pathlib.Path(input_path).parent.resolve())).write_pdf(output_path)

def main():
    if len(sys.argv) < 3:
        print("Usage: convert_to_pdf.py <input> <output>", file=sys.stderr)
        sys.exit(1)

    input_path, output_path = sys.argv[1], sys.argv[2]
    ext = pathlib.Path(input_path).suffix.lower()

    if ext == ".md":
        md_to_pdf(input_path, output_path)
    else:
        print(f"Unsupported format: {ext}", file=sys.stderr)
        sys.exit(1)

    print(f"Saved: {output_path}")

if __name__ == "__main__":
    main()
```

### Step 4: Run the Conversion

```bash
"$SKILL_DIR/.venv/bin/python" "$SKILL_DIR/convert_to_pdf.py" "<input_file>" "<output_file>"
```

If the command fails with a `weasyprint` system dependency error (e.g., missing `libpango`), install them first:

```bash
# Debian/Ubuntu
sudo apt-get install -y libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libffi-dev
```

Then retry the conversion.

### Step 5: Report Result

Confirm the output file was created and report its path. If conversion failed, show the error output clearly.
