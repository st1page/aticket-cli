"""Lease holder identity helpers.

Aticket uses the holder id only to decide whether a claim/release is the same
agent lease holder. The rendered ticket shows the holder label for humans.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .config import configured_session_root

GLOB_CHARS = "*?[]"
SESSION_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
AGENT_TYPE_HELP = (
    "Agent provider for the lease owner. Optional when CODEX_THREAD_ID or "
    "CLAUDE_CODE_SESSION_ID is available; stored internally as an agents:// xurl."
)
SESSION_ID_HELP = (
    "Agent session UUID. Optional when provider environment identifies the current "
    "agent; combined with --agent-type into agents://<agent-type>/<session-id> and verified locally."
)


@dataclass(frozen=True)
class HolderIdentity:
    holder_id: str
    holder_label: str
    holder_id_source: str


def is_holder_id_safe(holder_id: str) -> bool:
    return not any(ch in holder_id for ch in GLOB_CHARS)


def validate_holder_id_or_die(holder_id: str) -> None:
    if holder_id and is_holder_id_safe(holder_id):
        return
    raise SystemExit("Error: holder id is empty or contains glob characters (* ? [ ]). Refusing for safety.")


def _session_root(provider: str) -> Path:
    return configured_session_root(provider)


def _local_main_session_exists(provider: str, session_id: str) -> bool:
    root = _session_root(provider)
    if not root.is_dir():
        return False
    if provider == "codex":
        return any(path.is_file() for path in root.glob(f"*/*/*/*-{session_id}.jsonl"))
    if provider == "claude":
        return any(path.is_file() for path in root.glob(f"*/{session_id}.jsonl"))
    return False


def validate_owner_xurl_or_die(holder_id: str) -> tuple[str, str]:
    validate_holder_id_or_die(holder_id)
    parsed = urlparse(holder_id)
    provider = parsed.netloc
    parts = [part for part in parsed.path.split("/") if part]
    session_id = parts[0] if parts else ""
    if (
        parsed.scheme != "agents"
        or provider not in {"codex", "claude"}
        or len(parts) != 1
        or not SESSION_ID_RE.fullmatch(session_id)
    ):
        raise SystemExit(
            "Error: owner id must be a local xurl session URI in full agents:// form, like "
            "agents://codex/<session-id> or agents://claude/<session-id>."
        )
    if not _local_main_session_exists(provider, session_id):
        raise SystemExit(
            f"Error: owner xurl does not resolve to a local {provider} session: {holder_id}. "
            "Pass --agent-type and --session-id for an existing local agent session."
        )
    return provider, session_id


def owner_xurl_from_agent_session(agent_type: str, session_id: str) -> str:
    provider = agent_type.strip().lower()
    sid = session_id.strip()
    if provider not in {"codex", "claude"}:
        raise SystemExit("Error: --agent-type must be codex or claude.")
    if not SESSION_ID_RE.fullmatch(sid):
        raise SystemExit("Error: --session-id must be a UUID session id.")
    return f"agents://{provider}/{sid}"


def _parent_pid(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    close = stat.rfind(")")
    if close < 0:
        return None
    tail = stat[close + 1 :].strip().split()
    if len(tail) < 2:
        return None
    try:
        return int(tail[1])
    except ValueError:
        return None


def _argv_provider(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    argv = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
    for arg in argv[:3]:
        base = Path(arg).name.lower()
        if base in {"claude", "claude.exe"} or base.startswith("claude-"):
            return "claude"
        if base in {"codex", "codex.exe", "codex.js"} or base.startswith("codex-"):
            return "codex"
    return None


def _nearest_agent_provider() -> str | None:
    pid = os.getpid()
    seen: set[int] = set()
    for _ in range(24):
        if pid <= 1 or pid in seen:
            return None
        seen.add(pid)
        provider = _argv_provider(pid)
        if provider is not None:
            return provider
        parent = _parent_pid(pid)
        if parent is None:
            return None
        pid = parent
    return None


def _env_agent_session() -> tuple[str, str, str] | None:
    """Return provider/session/source inferred from the current agent env.

    Codex nested under Claude can inherit `CLAUDE_CODE_SESSION_ID`; Claude
    nested under Codex can inherit `CODEX_THREAD_ID`. When both ids are present,
    prefer the provider nearest in the process tree, then fall back to the
    session variables themselves.
    """
    codex_sid = os.environ.get("CODEX_THREAD_ID", "").strip()
    claude_sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()

    if codex_sid and claude_sid:
        nearest = _nearest_agent_provider()
        if nearest == "codex":
            return "codex", codex_sid, "env:CODEX_THREAD_ID"
        if nearest == "claude":
            return "claude", claude_sid, "env:CLAUDE_CODE_SESSION_ID"
    if os.environ.get("CODEX_CI") and codex_sid:
        return "codex", codex_sid, "env:CODEX_THREAD_ID"
    if os.environ.get("CLAUDECODE") and claude_sid:
        return "claude", claude_sid, "env:CLAUDE_CODE_SESSION_ID"
    if codex_sid:
        return "codex", codex_sid, "env:CODEX_THREAD_ID"
    if claude_sid:
        return "claude", claude_sid, "env:CLAUDE_CODE_SESSION_ID"
    return None


def infer_holder(*, agent_type: str = "", session_id: str = "", explicit_label: str = "") -> HolderIdentity:
    raw_label = explicit_label.strip() or os.environ.get("ATICKET_HOLDER_LABEL", "").strip()
    provider = (agent_type or "").strip()
    sid = (session_id or "").strip()
    if bool(provider) != bool(sid):
        raise SystemExit(
            "Error: pass both --agent-type <codex|claude> and --session-id <uuid>, "
            "or omit both so aticket can infer the current agent from CODEX_THREAD_ID "
            "or CLAUDE_CODE_SESSION_ID."
        )
    source = "arg:--agent-type/--session-id"
    if not provider and not sid:
        inferred = _env_agent_session()
        if inferred is None:
            raise SystemExit(
                "Error: aticket could not infer the current agent session. "
                "Run from a Codex/Claude tool subprocess with CODEX_THREAD_ID or "
                "CLAUDE_CODE_SESSION_ID, or pass --agent-type <codex|claude> and --session-id <uuid>."
            )
        provider, sid, source = inferred
    raw_id = owner_xurl_from_agent_session(provider, sid)
    provider, sid = validate_owner_xurl_or_die(raw_id)
    suffix = sid[:8]
    return HolderIdentity(raw_id, raw_label or f"{provider} ({suffix})", source)
