import sys
from argparse import Namespace
from pathlib import Path

import pytest

# Make the repo root importable so `import ticket_cli` works without install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_AGENT_TYPE = "codex"
DEFAULT_OWNER_SESSION_ID = "11111111-1111-4111-8111-111111111111"
DEFAULT_OWNER_URI = f"agents://{DEFAULT_AGENT_TYPE}/{DEFAULT_OWNER_SESSION_ID}"


@pytest.fixture()
def tickets_root(tmp_path, monkeypatch):
    """A throwaway AGENT_TICKETS_ROOT for each test."""
    root = tmp_path / "agent-tickets"
    root.mkdir()
    config = tmp_path / "aticket-config.toml"
    config.write_text("", encoding="utf-8")
    monkeypatch.setenv("AGENT_TICKETS_ROOT", str(root))
    monkeypatch.setenv("ATICKET_CONFIG", str(config))
    monkeypatch.setenv("CODEX_JSONL_ROOT", str(tmp_path / "codex-sessions"))
    monkeypatch.setenv("CLAUDE_JSONL_ROOT", str(tmp_path / "claude-projects"))
    create_codex_session(tmp_path / "codex-sessions", DEFAULT_OWNER_SESSION_ID)
    return root


def create_codex_session(root, session_id):
    path = Path(root) / "2026" / "06" / "05" / f"rollout-2026-06-05T00-00-00-{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")
    return path


def create_claude_session(root, session_id):
    path = Path(root) / "-code-tsshi-aticket-cli" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")
    return path


def owner_uri(session_id=DEFAULT_OWNER_SESSION_ID, provider="codex"):
    return f"agents://{provider}/{session_id}"


def owner_args(session_id=DEFAULT_OWNER_SESSION_ID, provider=DEFAULT_AGENT_TYPE):
    return ["--agent-type", provider, "--session-id", session_id]


def run(argv):
    """Invoke the CLI in-process; returns the exit code."""
    from ticket_cli.cli import main

    return main(argv)


def make_ticket(topic="demo"):
    from ticket_cli.ticket_new import create_ticket

    return create_ticket(
        topic,
        goal=f"Work on {topic}",
        agent_type=DEFAULT_AGENT_TYPE,
        session_id=DEFAULT_OWNER_SESSION_ID,
        holder_label="test holder",
    )


def make_stale_goal(ticket_dir, text="db-only goal"):
    """Update sqlite without rendering; used only to test stale-view guards."""
    from ticket_cli.lifecycle import cmd_change_goal

    return cmd_change_goal(
        Namespace(
            ticket=str(ticket_dir),
            payload=text,
            file="",
            no_render=True,
            format="plain",
        )
    )


def read_db(ticket_dir):
    from ticket_cli.db import open_db

    conn = open_db(Path(ticket_dir))
    try:
        meta = dict(conn.execute("SELECT * FROM ticket_meta LIMIT 1").fetchone())
        cv = dict(conn.execute("SELECT * FROM current_view WHERE singleton = 1").fetchone())
    finally:
        conn.close()
    return meta, cv


def seed_legacy_artifacts(ticket_dir, *entries):
    """Populate the legacy Artifacts section without using a public CLI command."""
    from ticket_cli.db import open_db
    from ticket_cli.render import do_render
    from ticket_cli.timeutil import now_iso

    body = "\n".join(f"- {entry}" for entry in entries)
    conn = open_db(Path(ticket_dir))
    try:
        now = now_iso()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE current_view SET artifacts_md = ?, updated_at = ? WHERE singleton = 1",
            (body, now),
        )
        conn.execute("UPDATE ticket_meta SET render_revision = render_revision + 1, updated_at = ?", (now,))
        conn.commit()
        do_render(Path(ticket_dir), conn)
    finally:
        conn.close()
