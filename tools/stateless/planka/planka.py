#!/usr/bin/env python3
"""Planka Kanban board integration.

Standalone stateless tool. Dependencies: requests.
"""

import argparse
import json
import os
import sys

# Allow importing core.secrets
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def _get_token(planka_url: str) -> str:
    """Authenticate with Planka and return a bearer token."""
    from core.secrets import get_credential
    import requests

    email = get_credential("PLANKA_EMAIL") or ""
    password = get_credential("PLANKA_PASSWORD") or ""
    if not email or not password:
        raise RuntimeError(
            "PLANKA_EMAIL and PLANKA_PASSWORD must be configured. "
            "Use set_secret to store them."
        )
    resp = requests.post(
        f"{planka_url}/api/access-tokens",
        json={"emailOrUsername": email, "password": password},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["item"]


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _get_planka_url() -> str:
    from core.secrets import get_credential
    return get_credential("PLANKA_URL") or "http://planka:1337"


# --- Mock data ---
_MOCK_PROJECTS = [
    {"id": "proj1", "name": "Development", "boards": [{"id": "board1", "name": "Sprint Board"}]}
]
_MOCK_BOARD = {
    "board": {"id": "board1", "name": "Sprint Board"},
    "lists": [{"id": "list1", "name": "To Do"}, {"id": "list2", "name": "Done"}],
    "cards": [{"id": "card1", "name": "Sample Card", "listId": "list1"}],
    "labels": [],
    "cardLabels": [],
}
_MOCK_CARD = {
    "card": {"id": "card1", "name": "Sample Card", "description": "A test card", "listId": "list1"},
    "labels": [],
    "cardLabels": [],
    "tasks": [],
    "attachments": [],
}


def cmd_list_projects(args: argparse.Namespace) -> int:
    if args.test_mode:
        print(json.dumps({"projects": _MOCK_PROJECTS}))
        return 0
    try:
        url = _get_planka_url()
        token = _get_token(url)
        import requests
        resp = requests.get(f"{url}/api/projects", headers=_headers(token), timeout=10)
        resp.raise_for_status()
        print(json.dumps({"projects": resp.json()["items"]}))
        return 0
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        return 1


def cmd_get_board(args: argparse.Namespace) -> int:
    if args.test_mode:
        print(json.dumps(_MOCK_BOARD))
        return 0
    try:
        url = _get_planka_url()
        token = _get_token(url)
        import requests
        resp = requests.get(f"{url}/api/boards/{args.board_id}", headers=_headers(token), timeout=10)
        resp.raise_for_status()
        data = resp.json()
        included = data.get("included", {})
        print(json.dumps({
            "board": data["item"],
            "lists": included.get("lists", []),
            "cards": included.get("cards", []),
            "labels": included.get("labels", []),
            "cardLabels": included.get("cardLabels", []),
        }))
        return 0
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        return 1


def cmd_get_card(args: argparse.Namespace) -> int:
    if args.test_mode:
        print(json.dumps(_MOCK_CARD))
        return 0
    try:
        url = _get_planka_url()
        token = _get_token(url)
        import requests
        resp = requests.get(f"{url}/api/cards/{args.card_id}", headers=_headers(token), timeout=10)
        resp.raise_for_status()
        data = resp.json()
        included = data.get("included", {})
        print(json.dumps({
            "card": data["item"],
            "labels": included.get("labels", []),
            "cardLabels": included.get("cardLabels", []),
            "tasks": included.get("tasks", []),
            "attachments": included.get("attachments", []),
        }))
        return 0
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        return 1


def cmd_move_card(args: argparse.Namespace) -> int:
    if args.test_mode:
        print(json.dumps({"moved": True, "card_id": args.card_id, "list_id": args.list_id}))
        return 0
    try:
        url = _get_planka_url()
        token = _get_token(url)
        import requests
        resp = requests.patch(
            f"{url}/api/cards/{args.card_id}",
            json={"listId": args.list_id, "position": 65535},
            headers=_headers(token),
            timeout=10,
        )
        resp.raise_for_status()
        print(json.dumps({"moved": True, "card_id": args.card_id, "list_id": args.list_id}))
        return 0
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        return 1


def cmd_add_comment(args: argparse.Namespace) -> int:
    if args.test_mode:
        print(json.dumps({"commented": True, "card_id": args.card_id}))
        return 0
    try:
        url = _get_planka_url()
        token = _get_token(url)
        import requests
        resp = requests.post(
            f"{url}/api/cards/{args.card_id}/comments",
            json={"text": args.text},
            headers=_headers(token),
            timeout=10,
        )
        resp.raise_for_status()
        print(json.dumps({"commented": True, "card_id": args.card_id}))
        return 0
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        return 1


def cmd_update_card(args: argparse.Namespace) -> int:
    if args.test_mode:
        print(json.dumps({"updated": True, "card_id": args.card_id}))
        return 0
    try:
        url = _get_planka_url()
        token = _get_token(url)
        import requests
        payload: dict[str, str] = {}
        if args.name:
            payload["name"] = args.name
        if args.description:
            payload["description"] = args.description
        if not payload:
            print(json.dumps({"error": "Nothing to update -- provide --name and/or --description."}))
            return 1
        resp = requests.patch(
            f"{url}/api/cards/{args.card_id}",
            json=payload,
            headers=_headers(token),
            timeout=10,
        )
        resp.raise_for_status()
        print(json.dumps({"updated": True, "card_id": args.card_id}))
        return 0
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        return 1


def cmd_assign_label(args: argparse.Namespace) -> int:
    if args.test_mode:
        print(json.dumps({"assigned": True, "card_id": args.card_id, "label_id": args.label_id}))
        return 0
    try:
        url = _get_planka_url()
        token = _get_token(url)
        import requests
        resp = requests.post(
            f"{url}/api/cards/{args.card_id}/card-labels",
            json={"labelId": args.label_id},
            headers=_headers(token),
            timeout=10,
        )
        resp.raise_for_status()
        print(json.dumps({"assigned": True, "card_id": args.card_id, "label_id": args.label_id}))
        return 0
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        return 1


def cmd_create_card(args: argparse.Namespace) -> int:
    if args.test_mode:
        print(json.dumps({
            "created": True,
            "list_id": args.list_id,
            "name": args.name,
            "card_type": args.card_type,
        }))
        return 0
    try:
        url = _get_planka_url()
        token = _get_token(url)
        import requests
        resp = requests.post(
            f"{url}/api/lists/{args.list_id}/cards",
            json={
                "name": args.name,
                "description": args.description or "",
                "position": 0,
                "type": args.card_type,
            },
            headers=_headers(token),
            timeout=10,
        )
        resp.raise_for_status()
        print(json.dumps(resp.json()["item"]))
        return 0
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Planka Kanban board tool")
    subparsers = parser.add_subparsers(dest="command", required=True)

    lp = subparsers.add_parser("list-projects", help="List all projects")
    lp.add_argument("--test-mode", action="store_true")

    gb = subparsers.add_parser("get-board", help="Get board details")
    gb.add_argument("--board-id", required=True)
    gb.add_argument("--test-mode", action="store_true")

    gc = subparsers.add_parser("get-card", help="Get card details")
    gc.add_argument("--card-id", required=True)
    gc.add_argument("--test-mode", action="store_true")

    mc = subparsers.add_parser("move-card", help="Move card to list")
    mc.add_argument("--card-id", required=True)
    mc.add_argument("--list-id", required=True)
    mc.add_argument("--test-mode", action="store_true")

    ac = subparsers.add_parser("add-comment", help="Add comment to card")
    ac.add_argument("--card-id", required=True)
    ac.add_argument("--text", required=True)
    ac.add_argument("--test-mode", action="store_true")

    uc = subparsers.add_parser("update-card", help="Update card")
    uc.add_argument("--card-id", required=True)
    uc.add_argument("--name", default="")
    uc.add_argument("--description", default="")
    uc.add_argument("--test-mode", action="store_true")

    al = subparsers.add_parser("assign-label", help="Assign label to card")
    al.add_argument("--card-id", required=True)
    al.add_argument("--label-id", required=True)
    al.add_argument("--test-mode", action="store_true")

    cc = subparsers.add_parser("create-card", help="Create a new card")
    cc.add_argument("--list-id", required=True)
    cc.add_argument("--name", required=True)
    cc.add_argument("--description", default="")
    cc.add_argument("--card-type", default="story")
    cc.add_argument("--test-mode", action="store_true")

    args = parser.parse_args()

    commands = {
        "list-projects": cmd_list_projects,
        "get-board": cmd_get_board,
        "get-card": cmd_get_card,
        "move-card": cmd_move_card,
        "add-comment": cmd_add_comment,
        "update-card": cmd_update_card,
        "assign-label": cmd_assign_label,
        "create-card": cmd_create_card,
    }

    handler = commands.get(args.command)
    if handler:
        return handler(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
