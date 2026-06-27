import json
import os
from pathlib import Path

import pytest

from conftest import DEFAULT_AGENT_TYPE, DEFAULT_OWNER_SESSION_ID, create_codex_session, owner_args, run
from ticket_cli.db import open_db
from ticket_cli.ticket_new import create_ticket


def _notice_rows(ticket_dir):
    conn = open_db(Path(ticket_dir))
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM notices ORDER BY id ASC").fetchall()]
    finally:
        conn.close()


def test_notice_send_renders_unchecked_notice(tickets_root):
    target = create_ticket(
        "target",
        goal="target goal",
        agent_type=DEFAULT_AGENT_TYPE,
        session_id=DEFAULT_OWNER_SESSION_ID,
        holder_label="target holder",
    )
    source = create_ticket(
        "source",
        goal="source goal",
        agent_type=DEFAULT_AGENT_TYPE,
        session_id=DEFAULT_OWNER_SESSION_ID,
        holder_label="source holder",
    )

    assert run([
        "message", "send",
        "--ticket", str(target),
        "please check reviewer note",
        "--from-ticket", str(source),
        "--with", "file:///tmp/review.txt",
        "--with", "https://github.example.com/org/repo/pull/123",
        *owner_args(),
        "--owner-label", "message sender",
    ]) == 0

    rows = _notice_rows(target)
    assert len(rows) == 1
    assert rows[0]["message"] == "please check reviewer note"
    assert rows[0]["from_ticket"] == Path(source).resolve().as_uri()
    assert rows[0]["sender_label"] == "message sender"

    md = (Path(target) / "TICKET.md").read_text()
    assert "## Messages" in md
    assert "Unread: 1" in md
    notice_lines = [line for line in md.splitlines() if "[message #1]" in line]
    assert len(notice_lines) == 1
    assert "please check reviewer note" in notice_lines[0]
    assert f"from={Path(source).resolve().as_uri()}" in notice_lines[0]
    assert "with=file:///tmp/review.txt" in notice_lines[0]
    assert "with=https://github.example.com/org/repo/pull/123" in notice_lines[0]


def test_notice_checked_log_is_single_line_with_metadata(tickets_root):
    target = create_ticket(
        "target",
        goal="target goal",
        agent_type=DEFAULT_AGENT_TYPE,
        session_id=DEFAULT_OWNER_SESSION_ID,
        holder_label="target holder",
    )
    source = create_ticket(
        "source",
        goal="source goal",
        agent_type=DEFAULT_AGENT_TYPE,
        session_id=DEFAULT_OWNER_SESSION_ID,
        holder_label="source holder",
    )

    assert run([
        "message", "send",
        "--ticket", str(target),
        "review summary is ready",
        "--from-ticket", str(source),
        "--with", "file:///tmp/review-summary.md",
        *owner_args(),
        "--owner-label", "message sender",
    ]) == 0
    assert run([
        "ticket", str(target), "message", "checked",
        "--until-id", "1",
        *owner_args(),
        "--owner-label", "checker",
    ]) == 0

    md = (Path(target) / "TICKET.md").read_text()
    notice_lines = [line for line in md.splitlines() if "[message #1]" in line]
    assert len(notice_lines) == 1
    line = notice_lines[0]
    assert "review summary is ready" in line
    assert f"from={Path(source).resolve().as_uri()}" in line
    assert "with=file:///tmp/review-summary.md" in line
    assert "by=agents://codex/" in line


def test_notice_send_does_not_warn_sender_about_target_unread_notice(tickets_root, capsys):
    target = create_ticket(
        "target",
        goal="target goal",
        agent_type=DEFAULT_AGENT_TYPE,
        session_id=DEFAULT_OWNER_SESSION_ID,
        holder_label="target holder",
    )

    assert run([
        "message", "send",
        "--ticket", str(target),
        "please inspect before archiving",
        *owner_args(),
        "--owner-label", "message sender",
    ]) == 0
    err = capsys.readouterr().err
    assert "appended to messages" in err
    assert "ticket has 1 unread message" not in err
    assert "## Messages" not in err


def test_unread_notice_warning_surfaces_to_target_holder_until_checked(tickets_root, capsys):
    target = create_ticket(
        "target",
        goal="target goal",
        agent_type=DEFAULT_AGENT_TYPE,
        session_id=DEFAULT_OWNER_SESSION_ID,
        holder_label="target holder",
    )

    assert run([
        "message", "send",
        "--ticket", str(target),
        "please inspect before archiving",
        *owner_args(),
        "--owner-label", "message sender",
    ]) == 0
    capsys.readouterr()

    assert run(["ticket", str(target), "log", "routine follow-up"]) == 0
    err = capsys.readouterr().err
    assert "ticket has 1 unread message" in err
    assert "[message #1]" in err

    assert run([
        "ticket", str(target), "message", "checked",
        "--until-id", "1",
        *owner_args(),
        "--owner-label", "checker",
    ]) == 0
    capsys.readouterr()

    assert run(["ticket", str(target), "log", "after check"]) == 0
    err = capsys.readouterr().err
    assert "unread message" not in err


def test_notice_checked_requires_active_holder(tickets_root):
    target = create_ticket(
        "target",
        goal="target goal",
        agent_type=DEFAULT_AGENT_TYPE,
        session_id=DEFAULT_OWNER_SESSION_ID,
        holder_label="target holder",
    )
    sender_session_id = "22222222-2222-4222-8222-222222222222"
    create_codex_session(Path(os.environ["CODEX_JSONL_ROOT"]), sender_session_id)

    assert run([
        "message", "send",
        "--ticket", str(target),
        "holder should see this",
        *owner_args(sender_session_id),
        "--owner-label", "external sender",
    ]) == 0

    with pytest.raises(SystemExit, match="only the active holder"):
        run([
            "ticket", str(target), "message", "checked",
            "--until-id", "1",
            *owner_args(sender_session_id),
            "--owner-label", "external sender",
        ])

    rows = _notice_rows(target)
    assert rows[0]["checked_at"] is None
    assert rows[0]["checked_by"] is None


def test_notice_checked_logs_and_hides_checked_notices_idempotently(tickets_root):
    target = create_ticket(
        "target",
        goal="target goal",
        agent_type=DEFAULT_AGENT_TYPE,
        session_id=DEFAULT_OWNER_SESSION_ID,
        holder_label="target holder",
    )
    source = create_ticket(
        "source",
        goal="source goal",
        agent_type=DEFAULT_AGENT_TYPE,
        session_id=DEFAULT_OWNER_SESSION_ID,
        holder_label="source holder",
    )

    for message in ("first notice", "second notice"):
        assert run([
            "message", "send",
            "--ticket", str(target),
            message,
            "--from-ticket", str(source),
            *owner_args(),
            "--owner-label", "message sender",
        ]) == 0

    assert run([
        "ticket", str(target), "message", "checked",
        "--until-id", "1",
        *owner_args(),
        "--owner-label", "checker",
    ]) == 0

    md = (Path(target) / "TICKET.md").read_text()
    assert "Unread: 1" in md
    assert "first notice" in md
    assert "second notice" in md
    assert md.count("[message #1]") == 1
    assert md.count("[message #2]") == 1
    assert "Checked messages until #1." in md

    assert run([
        "ticket", str(target), "message", "checked",
        "--until-id", "1",
        *owner_args(),
        "--owner-label", "checker",
    ]) == 0

    md_after_repeat = (Path(target) / "TICKET.md").read_text()
    assert md_after_repeat.count("[message #1]") == 1
    assert md_after_repeat.count("Checked messages until #1.") == 1

    assert run([
        "ticket", str(target), "message", "checked",
        "--until-id", "2",
        *owner_args(),
        "--owner-label", "checker",
    ]) == 0

    final_md = (Path(target) / "TICKET.md").read_text()
    assert "Unread: 1" not in final_md
    assert final_md.count("[message #1]") == 1
    assert final_md.count("[message #2]") == 1
    assert "Checked messages until #2." in final_md

    rows = _notice_rows(target)
    assert all(row["checked_at"] for row in rows)
    assert all(row["checked_by_label"] == "checker" for row in rows)


def test_notice_checked_noop_refreshes_stale_render(tickets_root):
    target = create_ticket(
        "target",
        goal="target goal",
        agent_type=DEFAULT_AGENT_TYPE,
        session_id=DEFAULT_OWNER_SESSION_ID,
        holder_label="target holder",
    )
    assert run([
        "message", "send",
        "--ticket", str(target),
        "already checked in db",
        *owner_args(),
        "--owner-label", "message sender",
    ]) == 0
    assert "already checked in db" in (Path(target) / "TICKET.md").read_text()

    conn = open_db(Path(target))
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE notices SET checked_at = '2026-06-07T00:00:00+08:00', checked_by = 'agents://codex/test'"
        )
        conn.execute("UPDATE ticket_meta SET render_revision = render_revision + 1")
        conn.commit()
    finally:
        conn.close()

    assert run([
        "ticket", str(target), "message", "checked",
        "--until-id", "1",
        *owner_args(),
        "--owner-label", "checker",
    ]) == 0

    refreshed_md = (Path(target) / "TICKET.md").read_text()
    assert "already checked in db" not in refreshed_md
    assert "Unread: 1" not in refreshed_md


def test_notice_message_escapes_newlines_to_one_rendered_line(tickets_root):
    target = create_ticket(
        "target",
        goal="target goal",
        agent_type=DEFAULT_AGENT_TYPE,
        session_id=DEFAULT_OWNER_SESSION_ID,
        holder_label="target holder",
    )
    message = "line one\n## Forged section\n- forged bullet\n\nline five"

    assert run([
        "message", "send",
        "--ticket", str(target),
        message,
        *owner_args(),
        "--owner-label", "message sender",
    ]) == 0

    rows = _notice_rows(target)
    assert len(rows) == 1
    assert rows[0]["message"] == "line one\\n## Forged section\\n- forged bullet\\n\\nline five"
    md = (Path(target) / "TICKET.md").read_text()
    notice_lines = [line for line in md.splitlines() if "[message #1]" in line]
    assert len(notice_lines) == 1
    assert "line one\\n## Forged section\\n- forged bullet\\n\\nline five" in notice_lines[0]


def test_notice_with_item_rejects_newlines(tickets_root):
    target = create_ticket(
        "target",
        goal="target goal",
        agent_type=DEFAULT_AGENT_TYPE,
        session_id=DEFAULT_OWNER_SESSION_ID,
        holder_label="target holder",
    )

    with pytest.raises(SystemExit, match="single-line URI"):
        run([
            "message", "send",
            "--ticket", str(target),
            "safe message",
            "--with", "file:///tmp/good\n## Forged Section\n- forged",
            *owner_args(),
            "--owner-label", "message sender",
        ])

    assert _notice_rows(target) == []


def test_notice_checked_noop_reports_no_change(tickets_root, capsys):
    target = create_ticket(
        "target",
        goal="target goal",
        agent_type=DEFAULT_AGENT_TYPE,
        session_id=DEFAULT_OWNER_SESSION_ID,
        holder_label="target holder",
    )

    capsys.readouterr()
    assert run([
        "ticket", str(target), "message", "checked",
        "--until-id", "1",
        *owner_args(),
        "--owner-label", "checker",
    ]) == 0

    err = capsys.readouterr().err
    assert "changed:         messages_checked" in err
    assert "(no change)" in err
    assert "appended to messages" not in err


def test_archive_refuses_unread_messages_until_checked(tickets_root):
    target = create_ticket(
        "target",
        goal="target goal",
        agent_type=DEFAULT_AGENT_TYPE,
        session_id=DEFAULT_OWNER_SESSION_ID,
        holder_label="target holder",
    )
    assert run([
        "message", "send",
        "--ticket", str(target),
        "read before archive",
        *owner_args(),
        "--owner-label", "message sender",
    ]) == 0

    with pytest.raises(SystemExit, match="refuse to archive: ticket has 1 unread message"):
        run(["ticket", str(target), "archive"])

    assert run([
        "ticket", str(target), "message", "checked",
        "--until-id", "1",
        *owner_args(),
        "--owner-label", "checker",
    ]) == 0
    assert run(["ticket", str(target), "archive"]) == 0


def test_archived_ticket_requires_allow_archived_for_historical_message(tickets_root, capsys):
    target = create_ticket(
        "target",
        goal="target goal",
        agent_type=DEFAULT_AGENT_TYPE,
        session_id=DEFAULT_OWNER_SESSION_ID,
        holder_label="target holder",
    )
    assert run(["ticket", str(target), "archive"]) == 0
    capsys.readouterr()

    with pytest.raises(SystemExit) as excinfo:
        run([
            "message", "send",
            "--ticket", str(target),
            "normal delivery after archive",
            *owner_args(),
            "--owner-label", "message sender",
        ])
    assert "ARCHIVED" in str(excinfo.value)
    assert "--allow-archived" in str(excinfo.value)
    assert _notice_rows(target) == []

    assert run([
        "message", "send",
        "--ticket", str(target),
        "historical context after archive",
        "--allow-archived",
        *owner_args(),
        "--owner-label", "message sender",
    ]) == 0
    cap = capsys.readouterr()
    assert "no active holder/session is guaranteed to receive it" in cap.err

    rows = _notice_rows(target)
    assert len(rows) == 1
    assert rows[0]["archived_delivery"] == 1
    assert rows[0]["checked_at"] is None

    md = (Path(target) / "TICKET.md").read_text()
    assert "## Messages" in md
    assert "Archived / no delivery guarantee:" in md
    assert "[message #1]" in md
    assert "historical context after archive" in md
    assert "Unread: 1" not in md

    with pytest.raises(SystemExit, match="ARCHIVED"):
        run([
            "ticket", str(target), "message", "checked",
            "--until-id", "1",
            *owner_args(),
            "--owner-label", "checker",
        ])

    assert run(["ticket", str(target), "archive"]) == 0


def test_message_send_to_non_ticket_dir_preserves_db_error(tmp_path):
    not_a_ticket = tmp_path / "not-a-ticket"
    not_a_ticket.mkdir()

    with pytest.raises(SystemExit) as excinfo:
        run([
            "message", "send",
            "--ticket", str(not_a_ticket),
            "hello",
            *owner_args(),
        ])
    assert "sqlite db not found" in str(excinfo.value)
    assert "--allow-archived" not in str(excinfo.value)


def test_archived_historical_message_json_is_machine_parseable(tickets_root, capsys):
    target = create_ticket(
        "target",
        goal="target goal",
        agent_type=DEFAULT_AGENT_TYPE,
        session_id=DEFAULT_OWNER_SESSION_ID,
        holder_label="target holder",
    )
    assert run(["ticket", str(target), "archive"]) == 0
    capsys.readouterr()

    assert run([
        "message", "send",
        "--ticket", str(target),
        "historical context after archive",
        "--allow-archived",
        *owner_args(),
        "--owner-label", "message sender",
        "--format", "json",
    ]) == 0
    cap = capsys.readouterr()
    payload = json.loads(cap.err)
    assert payload["change"]["appended_field"] == "messages"
    assert payload["change"]["delivery_warning"] == (
        "message appended to archived ticket; no active holder/session is guaranteed to receive it"
    )
    assert "no active holder/session" not in cap.err.replace(payload["change"]["delivery_warning"], "")

    rows = _notice_rows(target)
    assert len(rows) == 1
    assert rows[0]["archived_delivery"] == 1


def test_notice_rechecks_archived_state_inside_send_transaction(tickets_root, monkeypatch):
    target = create_ticket(
        "target",
        goal="target goal",
        agent_type=DEFAULT_AGENT_TYPE,
        session_id=DEFAULT_OWNER_SESSION_ID,
        holder_label="target holder",
    )

    import ticket_cli.notice as notice

    def archive_after_precheck(ticket_dir):
        conn = open_db(Path(ticket_dir))
        try:
            conn.execute("UPDATE ticket_meta SET lifecycle_state = 'ARCHIVED' WHERE ticket_dir = ?", (str(Path(ticket_dir).resolve()),))
            conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr(notice, "_require_message_send_not_archived", archive_after_precheck)

    with pytest.raises(SystemExit, match="ARCHIVED"):
        run([
            "message", "send",
            "--ticket", str(target),
            "late notice",
            *owner_args(),
            "--owner-label", "message sender",
        ])

    rows = _notice_rows(target)
    assert rows == []


def test_notice_rechecks_archived_state_inside_checked_transaction(tickets_root, monkeypatch):
    target = create_ticket(
        "target",
        goal="target goal",
        agent_type=DEFAULT_AGENT_TYPE,
        session_id=DEFAULT_OWNER_SESSION_ID,
        holder_label="target holder",
    )
    assert run([
        "message", "send",
        "--ticket", str(target),
        "pending notice",
        *owner_args(),
        "--owner-label", "message sender",
    ]) == 0

    import ticket_cli.notice as notice

    def archive_after_precheck(ticket_dir, *, action):
        conn = open_db(Path(ticket_dir))
        try:
            conn.execute(
                "UPDATE ticket_meta SET lifecycle_state = 'ARCHIVED' WHERE ticket_dir = ?",
                (str(Path(ticket_dir).resolve()),),
            )
            conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr(notice, "require_not_archived", archive_after_precheck)

    with pytest.raises(SystemExit, match="ARCHIVED"):
        run([
            "ticket", str(target), "message", "checked",
            "--until-id", "1",
            *owner_args(),
            "--owner-label", "checker",
        ])

    rows = _notice_rows(target)
    assert rows[0]["checked_at"] is None
