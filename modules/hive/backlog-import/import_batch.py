"""Apply one batch file of epic details, features and stories to the board.

Idempotent by title: an epic is matched by title and updated in place; a
feature or story already present under its parent with the same title is
updated rather than duplicated. Re-running a batch is a no-op plus any edits.
"""
import json
import sys

sys.path.insert(0, "/app/src")
import kanban_data as k

DETAILS = ("description", "requirements", "acceptance_criteria", "qa_tests")


def details(node):
    return {f: node[f] for f in DETAILS if node.get(f)}


def main(path):
    batch = json.load(open(path, encoding="utf-8"))
    data = k.get_data()
    epics = {e["title"]: e["id"] for e in data["epics"]}

    made = {"epic": 0, "feature": 0, "story": 0}
    touched = {"epic": 0, "feature": 0, "story": 0}

    for spec in batch["epics"]:
        title = spec["title"]
        epic_id = epics.get(title)
        if epic_id is None:
            epic_id = k.create_epic(title, **details(spec))["id"]
            epics[title] = epic_id
            made["epic"] += 1
        elif details(spec):
            k.update_epic(epic_id, **details(spec))
            touched["epic"] += 1

        live = k.get_data()
        feats = {
            f["title"]: f["id"]
            for f in live["features"]
            if f.get("epic_id") == epic_id
        }
        kids = {
            s["title"]: s["id"]
            for s in live["stories"]
            if s.get("epic_id") == epic_id
        }

        for fspec in spec.get("features", []):
            ftitle = fspec["title"]
            fid = feats.get(ftitle)
            if fid is None:
                fid = k.create_feature(epic_id, ftitle, **details(fspec))["id"]
                feats[ftitle] = fid
                made["feature"] += 1
            elif details(fspec):
                k.update_feature(fid, **details(fspec))
                touched["feature"] += 1

            under = {
                s["title"]: s["id"]
                for s in k.get_data()["stories"]
                if s.get("feature_id") == fid
            }
            for sspec in fspec.get("stories", []):
                sid = under.get(sspec["title"])
                if sid is None:
                    k.create_story(
                        sspec["title"],
                        feature_id=fid,
                        status=sspec.get("status", "backlog"),
                        **details(sspec),
                    )
                    made["story"] += 1
                else:
                    k.update_story(
                        sid, status=sspec.get("status", "backlog"), **details(sspec)
                    )
                    touched["story"] += 1

        for sspec in spec.get("stories", []):
            sid = kids.get(sspec["title"])
            if sid is None:
                k.create_story(
                    sspec["title"],
                    epic_id=epic_id,
                    status=sspec.get("status", "backlog"),
                    **details(sspec),
                )
                made["story"] += 1
            else:
                k.update_story(
                    sid, status=sspec.get("status", "backlog"), **details(sspec)
                )
                touched["story"] += 1

    print(f"created {made}  updated {touched}")
    final = k.get_data()
    print("board now:", {x: len(final[x]) for x in final})


if __name__ == "__main__":
    main(sys.argv[1])
