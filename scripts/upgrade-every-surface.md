# Retired: scripts/upgrade-every-surface.sh

It put every install of hive-surfaces on the newest commit of the old
repository's `main`. hive-mind's images no longer follow a branch: they build
from `modules/hive-surfaces` of the checkout, so rebuilding the services from
a checkout updates them (`docker compose build <services>` and recreate).
Edge minds name a version instead (see `modules/hive-surfaces/README.md`);
moving them is a later story, and until then they install from the old
repository's `main`.
