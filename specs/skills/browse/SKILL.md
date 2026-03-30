---
name: browse
description: "Browse the web interactively. Navigate pages, fill forms, click
  buttons, and extract content from JavaScript-rendered sites. Use when WebFetch
  fails or the task requires interaction (store lookups, form submissions, etc)."
argument-hint: "[url-or-task-description]"
tools: Read, Write, Bash
model: sonnet
user-invocable: true
---

# Browse

## When to Use
- Page requires JavaScript to render (SPAs, React, dynamic content)
- Task requires interaction (typing a zip code, clicking filters, submitting forms)
- WebFetch returns empty or useless HTML
- Need to navigate through multiple pages (search -> click result -> extract)

## When NOT to Use
- Static page with content in the HTML -> use WebFetch instead (faster, cheaper)
- API exists for the data -> use the API directly
- Searching for general knowledge -> use WebSearch (built-in) first

## Procedure

1. **Assess the task.** Decide whether it's a simple page read or multi-step
   interaction. If multi-step, plan the sequence before starting.

2. **Navigate.** Call `browser_navigate(url)`. Read the accessibility tree
   to understand the page structure. Look for:
   - Input fields (role: textbox, searchbox)
   - Buttons (role: button, link)
   - Navigation elements (tabs, menus)
   - Content areas (headings, text, lists)

3. **Interact.** Use `browser_type` for input fields, `browser_click` for
   buttons and links. After each interaction, check the result with
   `browser_content()`.

4. **Extract.** Once on the target page, use `browser_content(mode="text")`
   to get plain text, or `browser_content(mode="accessibility")` for
   structured data. Parse what you need.

5. **Clean up.** Call `browser_close()` when done, especially if the task
   is complete. Sessions auto-close after 5 minutes idle, but explicit
   cleanup is preferred.

## Selector Strategy

Prefer selectors in this order:
1. `role=button[name="Submit"]` -- most stable, accessibility-based
2. `text=Sign In` -- readable, works for visible text
3. `#element-id` -- fast, but IDs change across deploys
4. `.class-name` -- fragile, use as last resort

## Error Handling

- **Timeout**: Page didn't load -> try with `wait_until="domcontentloaded"`
  instead of `networkidle`
- **Selector not found**: Re-read the accessibility tree to find the correct
  element name or role
- **CAPTCHA**: Stop. Tell Daniel the site requires human verification. Do not
  attempt to solve CAPTCHAs.
- **Bot detection**: Try DuckDuckGo instead of Google. For persistent blocks,
  tell Daniel the site is not automatable.

## Web Search via Browser

For searches that need real browser rendering:
```
web_search("Honda mower HRN216", engine="duckduckgo")
```
Returns structured JSON with title, url, snippet. Use `browser_navigate`
on interesting results to read the full page.
