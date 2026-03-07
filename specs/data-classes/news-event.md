# Data Class: news-event

## Description
An external news event or world incident — recognizable by a dateline, breaking news, or reported occurrence. Not tied to a personal action by Daniel.

## Actions
- discard

## Notes
- Action is always discard — external events are not stored in memory
- Distinguished from `intention` (Daniel's own plans) and `timed-event` (scheduled events with a datetime)
- If Daniel explicitly engaged with the event or it directly affected him, reclassify to `preference`, `intention`, or `technical-config` as appropriate
- Examples: news headlines, political events, world incidents Daniel heard about
