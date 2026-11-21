"""Verified native recipient identities; UI text never grants control authority."""
from __future__ import annotations

import hashlib
import json
import re


class RecipientError(ValueError):
    def __init__(self, code: str, status: int = 409):
        super().__init__(code)
        self.code, self.status = code, status


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def identifier(value, name: str, limit: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
        raise RecipientError("invalid_" + name, 400)
    return value


def app_identity(window: dict) -> str | None:
    product, company = window.get("product"), window.get("company")
    if product == "Codex" and company in {"OpenAI OpCo, LLC", "OpenAI, L.L.C."}:
        return "codex_desktop"
    if product == "Claude" and company in {"Anthropic", "Anthropic, PBC"}:
        return "claude_code"
    return None


WINDOW_FIELDS = ("exe", "pid", "window_id", "process_started", "product", "company")

# Host-reviewed Codex Desktop selectors; a profile may replace each list. The
# native driver receives the same lists, so both sides resolve the same controls.
# Codex 26.1117 names the follow-up composer "Work with Codex" and keeps "Do anything"
# for a new chat. Its transcript is a "Conversation" group that also holds the
# composer's own unnamed group.
CODEX_COMPOSER_NAMES = ("Do anything", "Work with Codex")
CODEX_USER_MARKERS = ("You said:",)
CODEX_CONVERSATION_NAMES = ("Conversation",)


def codex_selectors(profile: dict | None = None) -> dict:
    profile = profile or {}
    return {"composer_names": list(profile.get("composer_names", CODEX_COMPOSER_NAMES)),
            "user_markers": list(profile.get("user_markers", CODEX_USER_MARKERS)),
            "conversation_names": list(profile.get("conversation_names", CODEX_CONVERSATION_NAMES))}


def window_identity(window: dict) -> dict:
    identity = {key: window.get(key) for key in WINDOW_FIELDS}
    if any(value in (None, "", 0) for value in identity.values()):
        raise RecipientError("unverified_window")
    return identity


def _has_marker(nodes, container, markers) -> bool:
    return any(n.get("parent") == container["id"] and n.get("role") == "Text"
               and n.get("name") in markers for n in nodes)


def _conversation_pane(nodes, by_id, box, markers, conversation_names) -> dict:
    """The one transcript pane bound to this composer, in either supported layout."""
    if not markers:
        raise RecipientError("unverified_conversation")
    # Single-pane layout: the composer and the transcript markers share a parent.
    if _has_marker(nodes, box, markers):
        return box
    # Conversation layout: an unnamed composer group directly inside the one named
    # conversation group that holds the transcript markers.
    outer = by_id.get(box.get("parent"))
    if (outer is None or box.get("role") != "Group" or box.get("name")
            or outer.get("role") != "Group" or outer.get("name") not in conversation_names
            or not _has_marker(nodes, outer, markers)):
        raise RecipientError("unverified_conversation")
    panes = [n for n in nodes if n.get("role") == "Group" and n.get("name") in conversation_names
             and _has_marker(nodes, n, markers)]
    if panes != [outer]:
        raise RecipientError("ambiguous_conversation")
    return outer


def recipient_view(snapshot: dict, profile: dict | None = None) -> dict:
    """Resolve only a currently exposed, unique conversation pane. Never navigate by title."""
    window = window_identity(snapshot["window"])
    app = app_identity(window)
    if app is None:
        raise RecipientError("unsupported_application")
    nodes = snapshot.get("nodes", [])
    if snapshot.get("truncated"):
        raise RecipientError("accessibility_truncated")
    if any(n.get("role") == "Unknown" and (n.get("invoke_supported") or n.get("value_supported"))
           for n in nodes):
        raise RecipientError("unsupported_accessibility")
    # Host-reviewed selectors are installation configuration, never request/model input.
    selectors = codex_selectors(profile) if app == "codex_desktop" else {
        "composer_names": (profile or {}).get("composer_names", []),
        "user_markers": (profile or {}).get("user_markers", []), "conversation_names": []}
    names, markers = selectors["composer_names"], selectors["user_markers"]
    if app == "claude_code" and not any(n.get("selected") and n.get("name") == "Code" for n in nodes):
        raise RecipientError("claude_code_pane_unverified")
    editors = [n for n in nodes if n.get("role") == "Edit" and n.get("name") in names
               and n.get("value_supported") and n.get("enabled") and not n.get("offscreen")]
    if len(editors) != 1:
        raise RecipientError("ambiguous_composer" if editors else "unsupported_accessibility")
    editor = editors[0]
    by_id = {n["id"]: n for n in nodes}
    box = by_id.get(editor.get("parent"))
    if box is None or box.get("role") not in {"Group", "Pane", "Document"}:
        raise RecipientError("unverified_task_pane")
    # Role-labelled transcript markers distinguish a conversation from a shell/editor.
    pane = _conversation_pane(nodes, by_id, box, markers, selectors["conversation_names"])
    peers = [n for n in nodes if n.get("parent") == pane["id"]]
    default_send_names = ["Send", "Send message", "Send prompt"]
    if app == "codex_desktop":
        default_send_names.append("Queue")
    send_names = (profile or {}).get("send_names", default_send_names)
    # The submit control is the composer's sibling in both layouts.
    submits = [n for n in nodes if n.get("parent") == box["id"] and n.get("role") == "Button"
               and n.get("name") in send_names
               and n.get("enabled") and n.get("invoke_supported") and not n.get("offscreen")]
    if len(submits) > 1:
        raise RecipientError("ambiguous_submit_control")
    denied = any(n.get("modal") or (n.get("role") == "Button" and n.get("name") in {
        "Approve", "Allow once", "Allow this session", "Run command", "Yes, proceed"}) for n in nodes)
    if denied:
        raise RecipientError("approval_surface_active")
    binding = snapshot.get("task_binding") or {}
    task_id = binding.get("task_id", "")
    if (app != "codex_desktop" or not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", task_id)
            or binding.get("deeplink") != "codex://threads/" + task_id
            or any(binding.get(key) != window[key] for key in ("pid", "window_id", "process_started"))
            or binding.get("composer_id") != editor["id"] or binding.get("pane_id") != pane["id"]):
        raise RecipientError("recipient_task_identity_unverified")
    identity = {**window, "app": app, "pane_id": pane["id"], "composer_id": editor["id"],
                "composer_name": editor["name"], "task_id": task_id, "deeplink": binding["deeplink"]}
    messages, current, parts = [], None, []
    for node in peers:
        if (app == "codex_desktop" and current is not None and node.get("role") == "Button"
                and node.get("name") == "Copy message" and node.get("invoke_supported")):
            if parts and re.fullmatch(r"(?:[1-9]|1[0-2]):[0-5][0-9] [AP]M", parts[-1]):
                current["text"] = "".join(parts[:-1])
            current = None
        if node.get("role") != "Text":
            continue
        name = node.get("name", "")
        if name in markers:
            current = {"id": node["id"], "text": ""}
            parts = []
            messages.append(current)
        elif name in {"ChatGPT said:", "Claude said:", "Assistant:"}:
            current = None
        elif current is not None and node.get("text_supported"):
            parts.append(node.get("text", name))
            current["text"] = "".join(parts)
    return {"identity": identity, "composer_text": editor.get("value", ""),
            "submit_id": submits[0]["id"] if submits else None, "messages": messages,
            "title": snapshot["window"].get("title", app), "nodes": nodes}
