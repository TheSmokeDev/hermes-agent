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


def window_identity(window: dict) -> dict:
    identity = {key: window.get(key) for key in WINDOW_FIELDS}
    if any(value in (None, "", 0) for value in identity.values()):
        raise RecipientError("unverified_window")
    return identity


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
    names = (profile or {}).get("composer_names", ["Do anything"] if app == "codex_desktop" else [])
    if app == "claude_code" and not any(n.get("selected") and n.get("name") == "Code" for n in nodes):
        raise RecipientError("claude_code_pane_unverified")
    editors = [n for n in nodes if n.get("role") == "Edit" and n.get("name") in names
               and n.get("value_supported") and n.get("enabled") and not n.get("offscreen")]
    if len(editors) != 1:
        raise RecipientError("ambiguous_composer" if editors else "unsupported_accessibility")
    editor = editors[0]
    by_id = {n["id"]: n for n in nodes}
    pane = by_id.get(editor.get("parent"))
    if pane is None or pane.get("role") not in {"Group", "Pane", "Document"}:
        raise RecipientError("unverified_task_pane")
    peers = [n for n in nodes if n.get("parent") == pane["id"]]
    # Role-labelled transcript markers distinguish a conversation from a shell/editor.
    markers = (profile or {}).get("user_markers", ["You said:"] if app == "codex_desktop" else [])
    if not markers or not any(n.get("name") in markers and n.get("role") == "Text" for n in peers):
        raise RecipientError("unverified_conversation")
    send_names = (profile or {}).get("send_names", ["Send", "Send message", "Send prompt"])
    submits = [n for n in peers if n.get("role") == "Button" and n.get("name") in send_names
               and n.get("invoke_supported") and not n.get("offscreen")]
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
    messages, current = [], None
    for node in peers:
        if node.get("role") != "Text":
            continue
        name = node.get("name", "")
        if name in markers:
            current = {"id": node["id"], "text": ""}
            messages.append(current)
        elif name in {"ChatGPT said:", "Claude said:", "Assistant:"}:
            current = None
        elif current is not None and node.get("text_supported"):
            current["text"] += node.get("text", name)
    return {"identity": identity, "composer_text": editor.get("value", ""),
            "submit_id": submits[0]["id"] if submits else None, "messages": messages,
            "title": snapshot["window"].get("title", app), "nodes": nodes}
