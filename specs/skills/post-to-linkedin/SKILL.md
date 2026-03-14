---
name: post-to-linkedin
description: Post to Daniel's LinkedIn. Reads a post file from /usr/src/app/linkedin/, checks style, presents for approval, then posts via MCP. Use when Daniel wants to publish a LinkedIn post.
user-invocable: true
argument-hint: "[post filename]"
tools: Read, Glob, mcp__hive-mind-mcp__post_to_linkedin
---

# Post to LinkedIn

## Step 1 — Identify the post file

If a filename was passed as an argument, use it. Otherwise list `/usr/src/app/linkedin/` and ask Daniel which file to post. Ignore `style.md`, `old_posts.md`, `DEBUG.md`, and `architecture.md`.

## Step 2 — Read post and style guide

Read the post file and `/usr/src/app/linkedin/style.md` in parallel. The style guide is the source of truth for tone, formatting, and voice — all posts must conform to it.

## Step 3 — Style review and edit

Check the post against the style guide. Fix any violations silently. Then present the full edited post body to Daniel exactly as it will be sent to LinkedIn.

## Step 4 — Check for image

If the post references an image file (e.g. a `.png` or `.jpg` in the LinkedIn folder), note the container path: `/projects/hive_mind/linkedin/{filename}`.

**Critical constraint:** Images and videos always render at the **bottom** of the post on LinkedIn, regardless of placement in the text. Warn Daniel if the post text references the image in a way that assumes it appears inline — the text should be written so the image at the bottom makes sense.

## Step 5 — Get approval

Ask Daniel: "Ready to post?" Do not proceed without explicit confirmation.

## Step 6 — Post

Call `post_to_linkedin` with:
- `content`: the full post text (links can be embedded inline — LinkedIn converts them to shortened links)
- `image_path`: container path to image if one is attached, otherwise omit

## Step 7 — Confirm and archive

Report the post ID on success.

Then move the post file and any associated image/video into an archive folder:

```
/usr/src/app/linkedin/posted/YYYY-MM-DD_<slug>/
```

Where `<slug>` is a 3-5 word kebab-case summary of the post (e.g. `hive-mind-is-back`, `ada-voice-intro`). Use today's date.

Copy the markdown file and any image/video that was attached into that folder. Do not delete the originals until the copy succeeds. Then delete the originals from the LinkedIn root.
