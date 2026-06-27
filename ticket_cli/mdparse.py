"""Parse a rendered TICKET.md back into sections.

Greenfield `init` starts blank from the template, so this parser exists mainly
for fork inheritance (a fork reads the source ticket's rendered sections) and
for any external tooling that wants to read a ticket without opening sqlite.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .paths import MANAGED_MARKER

# Fork metadata serialized into the ## Fork section and state/fork.json.
FORK_SCALAR_PREFIXES = {
    "Forked from": "forked_from",
    "Snapshot taken at": "snapshot_taken_at",
    "Source snapshot": "source_ticket_snapshot_path",
    "Canonical source after fork": "canonical_source_after_fork",
    "Source ticket owner at fork time": "source_ticket_owner_at_fork_time",
}
FORK_LIST_PREFIXES = {
    "Copied path": "copied_paths",
}


def parse_ticket_md(ticket_md: Path) -> tuple[str, dict[str, str]]:
    """Parse TICKET.md into (title_line, {section_name: body})."""
    lines = ticket_md.read_text(encoding="utf-8").splitlines()
    if lines and lines[0].strip() == MANAGED_MARKER:
        lines = lines[1:]
    while lines and (
        lines[-1].startswith("Rendered from DB revision:")
        or lines[-1].startswith("Rendered at:")
        or lines[-1].strip() == "---"
        or lines[-1].strip() == ""
    ):
        lines.pop()

    title = lines[0] if lines else "# Ticket"
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines[1:]:
        if line.startswith("## "):
            current = line[3:].strip()
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    rendered = {name: "\n".join(body).strip("\n") for name, body in sections.items()}
    return title, rendered


def title_from_dir(ticket_dir: Path) -> str:
    name = ticket_dir.name
    m = re.match(r"^(\d{4}-\d{2}-\d{2})-(.+)-(\d{6})$", name)
    if m:
        date, topic, _ = m.groups()
        return f"# Ticket: {date} {topic}"
    return f"# Ticket: {name}"


def parse_fork_section(body: str) -> dict:
    data: dict[str, object] = {}
    for raw in body.splitlines():
        line = raw.strip()
        if not line.startswith("- "):
            continue
        content = line[2:]
        matched = False
        for prefix, key in FORK_SCALAR_PREFIXES.items():
            marker = f"{prefix}: "
            if content.startswith(marker):
                data[key] = content[len(marker):].strip()
                matched = True
                break
        if matched:
            continue
        for prefix, key in FORK_LIST_PREFIXES.items():
            marker = f"{prefix}: "
            if content.startswith(marker):
                data.setdefault(key, [])
                data[key].append(content[len(marker):].strip())  # type: ignore[attr-defined]
                break
    return data


def load_fork_metadata(ticket_dir: Path, sections: dict[str, str]) -> dict | None:
    parsed = parse_fork_section(sections.get("Fork", ""))
    fork_json = ticket_dir / "state" / "fork.json"
    if fork_json.exists():
        try:
            fork_data = json.loads(fork_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            fork_data = {}
        for key in FORK_SCALAR_PREFIXES.values():
            value = fork_data.get(key)
            if value not in (None, ""):
                parsed[key] = value
        for key in FORK_LIST_PREFIXES.values():
            values = fork_data.get(key)
            if isinstance(values, list) and values:
                parsed[key] = values
    return parsed or None


def dump_fork_metadata(fork_metadata: dict | None) -> str | None:
    if not fork_metadata:
        return None
    return json.dumps(fork_metadata, ensure_ascii=False, sort_keys=True)


def format_fork_section(fork_metadata_json: str | None) -> str:
    if not fork_metadata_json:
        return ""
    try:
        data = json.loads(fork_metadata_json)
    except json.JSONDecodeError:
        return ""
    lines: list[str] = []
    ordered_scalars = [
        ("forked_from", "Forked from"),
        ("snapshot_taken_at", "Snapshot taken at"),
        ("source_ticket_snapshot_path", "Source snapshot"),
        ("canonical_source_after_fork", "Canonical source after fork"),
        ("source_ticket_owner_at_fork_time", "Source ticket owner at fork time"),
    ]
    for key, label in ordered_scalars:
        value = str(data.get(key) or "").strip()
        if key == "forked_from" and value and not value.endswith("/"):
            value = f"{value}/"
        if value:
            lines.append(f"- {label}: {value}")
    for key, label in (("copied_paths", "Copied path"),):
        values = data.get(key) or []
        if isinstance(values, list):
            lines.extend(f"- {label}: {str(value).strip()}" for value in values if str(value).strip())
    return "\n".join(lines).strip()
