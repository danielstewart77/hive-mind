"""Planka Kanban board integration.

Provides tools for reading and managing cards in the Hive Mind's Planka board.
Planka runs at http://planka:1337 on the hivemind Docker network.

Required secrets (set via set_secret):
  PLANKA_EMAIL    — Planka admin email
  PLANKA_PASSWORD — Planka admin password

Optional env var:
  PLANKA_URL — defaults to http://planka:1337
"""

import json
import os

import requests
from agent_tooling import tool
from agents.secret_manager import get_credential

PLANKA_URL = get_credential("PLANKA_URL") or "http://planka:1337"

# Development board label IDs
LABEL_ADA = "1720207192893686912"
LABEL_DANIEL = "1720605269303493825"
LABEL_LOW_PRIORITY = "1720174481533568072"


def _get_token() -> str:
    """Authenticate with Planka and return a bearer token."""
    email = get_credential("PLANKA_EMAIL") or ""
    password = get_credential("PLANKA_PASSWORD") or ""
    if not email or not password:
        raise RuntimeError(
            "PLANKA_EMAIL and PLANKA_PASSWORD must be configured. "
            "Use set_secret to store them."
        )
    resp = requests.post(
        f"{PLANKA_URL}/api/access-tokens",
        json={"emailOrUsername": email, "password": password},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["item"]


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@tool(tags=["data"])
def planka_list_projects() -> str:
    """List all Planka projects and their boards.

    Returns:
        JSON list of projects, each with id, name, and boards array.
    """
    try:
        token = _get_token()
        resp = requests.get(
            f"{PLANKA_URL}/api/projects",
            headers=_headers(token),
            timeout=10,
        )
        resp.raise_for_status()
        return json.dumps(resp.json()["items"])
    except Exception as e:
        return f"Error: {e}"


@tool(tags=["data"])
def planka_get_board(board_id: str) -> str:
    """Get a Planka board with its lists and card summaries.

    Args:
        board_id: The Planka board ID.

    Returns:
        JSON with board details, lists (columns), and card summaries.
    """
    try:
        token = _get_token()
        resp = requests.get(
            f"{PLANKA_URL}/api/boards/{board_id}",
            headers=_headers(token),
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        included = data.get("included", {})
        return json.dumps({
            "board": data["item"],
            "lists": included.get("lists", []),
            "cards": included.get("cards", []),
            "labels": included.get("labels", []),
            "cardLabels": included.get("cardLabels", []),
        })
    except Exception as e:
        return f"Error: {e}"


@tool(tags=["data"])
def planka_get_card(card_id: str) -> str:
    """Get a Planka card's full details including description, labels, and checklists.

    Args:
        card_id: The Planka card ID.

    Returns:
        JSON with card details: id, name, description, listId, dueDate, labels, etc.
    """
    try:
        token = _get_token()
        resp = requests.get(
            f"{PLANKA_URL}/api/cards/{card_id}",
            headers=_headers(token),
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        included = data.get("included", {})
        return json.dumps({
            "card": data["item"],
            "labels": included.get("labels", []),
            "cardLabels": included.get("cardLabels", []),
            "tasks": included.get("tasks", []),
            "attachments": included.get("attachments", []),
        })
    except Exception as e:
        return f"Error: {e}"


@tool(tags=["storage"])
def planka_move_card(card_id: str, list_id: str) -> str:
    """Move a Planka card to a different list (column).

    Args:
        card_id: The Planka card ID.
        list_id: The target list (column) ID.

    Returns:
        Confirmation message or error.
    """
    try:
        token = _get_token()
        resp = requests.patch(
            f"{PLANKA_URL}/api/cards/{card_id}",
            json={"listId": list_id, "position": 65535},
            headers=_headers(token),
            timeout=10,
        )
        resp.raise_for_status()
        return f"Card {card_id} moved to list {list_id}."
    except Exception as e:
        return f"Error: {e}"


@tool(tags=["storage"])
def planka_add_comment(card_id: str, text: str) -> str:
    """Add a comment to a Planka card.

    Args:
        card_id: The Planka card ID.
        text: The comment text (markdown supported).

    Returns:
        Confirmation message or error.
    """
    try:
        token = _get_token()
        resp = requests.post(
            f"{PLANKA_URL}/api/cards/{card_id}/comments",
            json={"text": text},
            headers=_headers(token),
            timeout=10,
        )
        resp.raise_for_status()
        return f"Comment added to card {card_id}."
    except Exception as e:
        return f"Error: {e}"


@tool(tags=["storage"])
def planka_update_card(card_id: str, name: str = "", description: str = "") -> str:
    """Update a Planka card's title and/or description.

    Args:
        card_id: The Planka card ID.
        name: New card title (omit to leave unchanged).
        description: New card description in markdown (omit to leave unchanged).

    Returns:
        Confirmation message or error.
    """
    try:
        token = _get_token()
        payload = {}
        if name:
            payload["name"] = name
        if description:
            payload["description"] = description
        if not payload:
            return "Nothing to update — provide name and/or description."
        resp = requests.patch(
            f"{PLANKA_URL}/api/cards/{card_id}",
            json=payload,
            headers=_headers(token),
            timeout=10,
        )
        resp.raise_for_status()
        return f"Card {card_id} updated."
    except Exception as e:
        return f"Error: {e}"


@tool(tags=["storage"])
def planka_assign_label(card_id: str, label_id: str) -> str:
    """Assign an existing label to a Planka card.

    Args:
        card_id: The Planka card ID.
        label_id: The label ID to assign.

    Returns:
        Confirmation message or error.
    """
    try:
        token = _get_token()
        resp = requests.post(
            f"{PLANKA_URL}/api/cards/{card_id}/card-labels",
            json={"labelId": label_id},
            headers=_headers(token),
            timeout=10,
        )
        resp.raise_for_status()
        return f"Label {label_id} assigned to card {card_id}."
    except Exception as e:
        return f"Error: {e}"


@tool(tags=["storage"])
def planka_create_card(list_id: str, name: str, description: str = "", card_type: str = "story") -> str:
    """Create a new card in a Planka list.

    Args:
        list_id: The list (column) ID to create the card in.
        name: Card title.
        description: Optional card description (markdown supported).
        card_type: Card type — "story" (default) or "project".

    Returns:
        JSON with the created card's id and details.
    """
    try:
        token = _get_token()
        resp = requests.post(
            f"{PLANKA_URL}/api/lists/{list_id}/cards",
            json={"name": name, "description": description, "position": 0, "type": card_type},
            headers=_headers(token),
            timeout=10,
        )
        resp.raise_for_status()
        return json.dumps(resp.json()["item"])
    except Exception as e:
        return f"Error: {e}"
