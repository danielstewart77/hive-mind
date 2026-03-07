# Data Class: news-digest

## Description
Raw news content from digests, newsletters, or news feed runs — TLDR, X AI Lurker reports, InfoSec roundups, and similar. Recognizable by a list of headlines, summaries, or story clusters with no personal engagement. Always discarded as-stored.

## Actions
- discard

## Notes
- Always discard — news content has no lasting retrieval value on its own
- Distinguished from `ephemeral` (point-in-time live data) by source: this is curated news, not live API data
- If Daniel engaged with a story — said it's interesting, decided to act on it, or it triggered a decision — that chunk is NOT news-digest. It has transformed into something else:
  - A project decision → `future-project`
  - A system change we're implementing → `technical-config`
  - A preference Daniel expressed → `preference`
  - An observation about the world that matters to Daniel → `world-event`
- The transformation happens at classification time based on whether the content includes engagement/decision language, not based on whether it came from a newsletter
- Examples of news-digest: TLDR summary blobs, X AI Lurker run reports, InfoSec roundup lists, raw headline batches
