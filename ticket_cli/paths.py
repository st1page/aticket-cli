"""Path conventions and the AGENT_TICKETS_ROOT resolution.

    $AGENT_TICKETS_ROOT/            # default /code/tsshi/agent-tickets
    └── tickets/                    # canonical ticket dirs (stable path)
        └── <YYYY-MM-DD>-<topic>-<HHMMSS>/
"""
from __future__ import annotations

import re
from pathlib import Path

from .config import AGENT_TICKETS_ROOT_DEFAULT, configured_tickets_root

MANAGED_MARKER = "<!-- managed-by: sqlite -->"

CANONICAL_TICKETS_DIRNAME = "tickets"

ALLOWED_LIFECYCLE_STATES = ("BACKLOG", "ACTIVE", "ARCHIVED")


def agent_tickets_root(raw: str | None = None) -> Path:
    return configured_tickets_root(raw)


def canonical_tickets_dir(root: Path) -> Path:
    return root / CANONICAL_TICKETS_DIRNAME


def canonical_ticket_dir(root: Path, ticket_name: str) -> Path:
    return canonical_tickets_dir(root) / ticket_name


def normalize_lifecycle_state(raw: str) -> str:
    state = (raw or "").strip().upper()
    if state not in ALLOWED_LIFECYCLE_STATES:
        raise SystemExit(f"unsupported lifecycle state: {raw}")
    return state


def is_item_uri(raw: str) -> bool:
    """Return whether raw text has a URI-like scheme."""
    return re.match(r"^[A-Za-z][A-Za-z0-9_.+-]*://", raw.strip()) is not None


def split_links(links_body: str) -> tuple[list[str], str]:
    """Split a ## Items body into (raw_uri_items, extra_markdown)."""
    items: list[str] = []
    extra_lines: list[str] = []
    for raw in links_body.splitlines():
        line = raw.strip()
        if not line:
            continue
        candidate = line[2:] if line.startswith("- ") else line
        if is_item_uri(candidate):
            items.append(candidate)
            continue
        extra_lines.append(line if line.startswith("- ") else f"- {line}")
    return items, "\n".join(extra_lines).strip()
