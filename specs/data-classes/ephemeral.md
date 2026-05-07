# Data Class: ephemeral

## Description
A chunk lands here if it does not match any of `current-state`,
`future-state`, or `feedback`. Includes time-bounded data (weather lookups,
live prices, query snapshots), news headlines and digests with no
engagement, world events Daniel did not act on, Planka task events that
are pure operational records, and anything that doesn't fit one of the
three storage classes.

The classifier evaluates the three storage classes first; ephemeral is
the fall-through.

## Actions
- discard

## Pruning
- strategy: none
