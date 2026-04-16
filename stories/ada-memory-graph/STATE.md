# Story State Tracker

**Story:** Ada memory graph — sparktobloom.com /graph page  
**Card ID:** 1753188531083150484  
**Branch:** story/ada-memory-graph

## Progress

- [state 1][X] Pull story from Planka
- [state 2][X] Create implementation plan
- [state 3][X] Implement with TDD
- [state 4][X] Code review
- [state 5][ ] Ready for merge

## Acceptance Criteria

- [ ] Docker-compose setup: Add `hivemind-data` volume to `spark_to_bloom/docker-compose.yml` as read-only mount
- [ ] `/graph/data` endpoint: FastAPI route returns JSON graph structure with nodes and edges from lucent.db
- [ ] `/graph/data` endpoint: Uses stdlib `sqlite3` only (no external drivers)
- [ ] `/graph/data` endpoint: Queries up to 400 nodes and their edges efficiently
- [ ] `/graph` route: FastAPI endpoint returns graph.html template
- [ ] graph.html template: Renders Cytoscape.js force-directed visualization (`cose` layout)
- [ ] graph.html template: Extends layout.html properly (includes base styling, navigation)
- [ ] Node colors: Agent = gold, Memory = blue, Person = green, Concept = purple
- [ ] Interactive features: Click node → sidebar shows name + type + properties
- [ ] Interactive features: Zoom, pan, drag built-in (Cytoscape.js default)
- [ ] layout.html: Add "graph" link to navigation menu
- [ ] No new dependencies required (Cytoscape.js via CDN)
- [ ] Page is public and read-only (no write operations)

## Dependencies

- **Blocker:** Loose End #11 (Lucent) must be complete before implementing this story
- lucent.db must be populated with node and edge data
- hive_mind docker-compose must expose `hive_mind_data` volume

## Context

This story assumes the Lucent migration (from Neo4j to SQLite) is complete. The `/graph/data` endpoint queries the SQLite database directly via a read-only volume mount. No external graph drivers, no network hops, no credentials.

See: `plans/plugin-setup-loose-ends.md` section "10. Ada's Memory Graph — Public Read-Only View on sparktobloom.com"
