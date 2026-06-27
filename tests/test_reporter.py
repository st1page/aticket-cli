"""Reporter output for the current agent-facing write commands."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from conftest import make_ticket, owner_args, read_db, run
from ticket_cli.notice_alert import attach_unread_messages_to_json


def _capture(capsys):
    return capsys.readouterr().err


def _capture_both(capsys):
    return capsys.readouterr()


def test_new_stdout_is_path_only_and_requires_goal(tickets_root, capsys):
    with pytest.raises(SystemExit):
        run(["ticket", "new", "--topic", "no-goal", *owner_args(), "--owner-label", "tester"])

    rc = run([
        "ticket", "new", "--topic", "alpha", "--goal", "prove the command surface",
        "--short-context", "fresh ticket",
        *owner_args(), "--owner-label", "tester",
    ])
    assert rc == 0
    cap = _capture_both(capsys)
    stdout_lines = cap.out.splitlines()
    assert len(stdout_lines) == 1
    assert Path(stdout_lines[0]).is_dir()
    assert "ticket: " in cap.err
    assert "lifecycle:       ACTIVE" in cap.err
    assert "short_context:" in cap.err
    assert "fresh ticket" in cap.err


def test_new_json_format_writes_json_to_stderr_path_to_stdout(tickets_root, capsys):
    rc = run([
        "ticket", "new", "--topic", "json-probe", "--goal", "json goal",
        *owner_args(), "--owner-label", "tester", "--format", "json",
    ])
    assert rc == 0
    cap = _capture_both(capsys)
    assert len(cap.out.splitlines()) == 1
    payload = json.loads(cap.err)
    assert payload["change"]["changed_field"] == "lifecycle"
    assert payload["snapshot"]["lifecycle"] == "ACTIVE"
    assert payload["snapshot"]["goal"] == "json goal"
    assert payload["snapshot"]["owner"].startswith("claim by tester at ")


def test_change_goal_overwrite_shows_old_to_new(tickets_root, capsys):
    t = make_ticket()
    capsys.readouterr()
    run(["ticket", str(t), "goal", "v1"])
    run(["ticket", str(t), "goal", "v2"])
    out = _capture(capsys)
    assert "changed:         goal" in out
    assert '"v1" → "v2"' in out


def test_change_goal_truncates_long_value(tickets_root, capsys):
    long_text = "x" * 200
    t = make_ticket()
    capsys.readouterr()
    run(["ticket", str(t), "goal", long_text])
    out = _capture(capsys)
    assert "200 chars total" in out
    assert long_text not in out


def test_short_context_replaces_recovery_summary(tickets_root, capsys):
    t = make_ticket()
    capsys.readouterr()
    run(["ticket", str(t), "context", "waiting for review; rerun pytest next"])
    out = _capture(capsys)
    assert "changed:         short_context" in out
    assert "waiting for review" in out


def test_archive_sets_lifecycle_and_noop_marks_no_change(tickets_root, capsys):
    t = make_ticket()
    capsys.readouterr()
    run(["ticket", str(t), "archive"])
    out = _capture(capsys)
    assert '"ACTIVE" → "ARCHIVED"' in out
    assert "lifecycle:       ARCHIVED" in out
    run(["ticket", str(t), "archive"])
    out = _capture(capsys)
    assert '"ARCHIVED" → "ARCHIVED"  (no change)' in out


def test_log_and_add_item_append_snapshots(tickets_root, capsys):
    t = make_ticket()
    capsys.readouterr()
    run(["ticket", str(t), "log", "first event"])
    out = _capture(capsys)
    assert "appended to work_log" in out
    assert "added 1" in out

    run(["ticket", str(t), "add-item", "https://example.test/pr/1"])
    out = _capture(capsys)
    assert "appended to items" in out
    assert "added 1" in out


def test_remember_appends_to_must_remember_section(tickets_root, capsys):
    t = make_ticket("remember-demo")
    capsys.readouterr()

    run(["ticket", str(t), "remember", "Preflight: verify linked worktree before editing"])
    out = _capture(capsys)
    assert "appended to must_remember" in out
    assert "added 1" in out

    md = (Path(t) / "TICKET.md").read_text(encoding="utf-8")
    assert "## Must remember" in md
    assert "1. Preflight: verify linked worktree before editing" in md

    _, cv = read_db(t)
    assert json.loads(cv["must_remember"]) == ["Preflight: verify linked worktree before editing"]


def test_forget_deletes_must_remember_entry_by_index(tickets_root, capsys):
    t = make_ticket("forget-demo")
    run(["ticket", str(t), "remember", "First invariant"])
    run(["ticket", str(t), "remember", "Second invariant"])
    capsys.readouterr()

    run(["ticket", str(t), "forget", "1"])
    out = _capture(capsys)
    assert "changed:         must_remember" in out

    _, cv = read_db(t)
    assert json.loads(cv["must_remember"]) == ["Second invariant"]
    md = (Path(t) / "TICKET.md").read_text(encoding="utf-8")
    assert "First invariant" not in md
    assert "1. Second invariant" in md


def test_ticket_commands_remind_about_must_remember_entries(tickets_root, capsys):
    t = make_ticket("remember-reminder")
    run(["ticket", str(t), "remember", "Preflight: read brief before editing"])
    capsys.readouterr()

    run(["ticket", str(t), "log", "routine work"])
    err = capsys.readouterr().err

    assert "must remember: ticket has 1/16 entries" in err
    assert "1. Preflight: read brief before editing" in err


def test_brief_prints_recovery_preflight_summary(tickets_root, capsys):
    t = make_ticket("brief-demo")
    run(["ticket", str(t), "context", "current state and next move"])
    run(["ticket", str(t), "remember", "Invariant: keep root checkout clean"])
    run([
        "message", "send",
        "--ticket", str(t),
        "review note waiting",
        *owner_args(),
    ])
    capsys.readouterr()

    run(["ticket", str(t), "brief"])
    cap = capsys.readouterr()

    assert "ticket:" in cap.out
    assert "current state and next move" in cap.out
    assert "must remember:   1/16" in cap.out
    assert "1. Invariant: keep root checkout clean" in cap.out
    assert "unread messages: 1" in cap.out
    assert "review note waiting" in cap.out
    assert "must remember: ticket has" not in cap.err


def test_brief_json_includes_must_remember_and_messages(tickets_root, capsys):
    t = make_ticket("brief-json")
    run(["ticket", str(t), "remember", "Preflight: json carries this"])
    run([
        "message", "send",
        "--ticket", str(t),
        "json message",
        *owner_args(),
    ])
    capsys.readouterr()

    run(["ticket", str(t), "brief", "--format", "json"])
    cap = capsys.readouterr()
    payload = json.loads(cap.out)

    assert payload["must_remember"]["items"] == ["Preflight: json carries this"]
    assert payload["must_remember"]["limit"] == 16
    assert payload["unread_messages"]["count"] == 1
    assert payload["unread_messages"]["messages"][0]["message"] == "json message"


def test_remember_requires_deleting_before_exceeding_16_entries(tickets_root):
    t = make_ticket("remember-limit")
    for idx in range(16):
        run(["ticket", str(t), "remember", f"Rule {idx + 1}"])

    with pytest.raises(SystemExit, match="Must remember is full"):
        run(["ticket", str(t), "remember", "Rule 17"])

    _, cv = read_db(t)
    assert len(json.loads(cv["must_remember"])) == 16


def test_log_requires_input(tickets_root, capsys):
    t = make_ticket()
    capsys.readouterr()
    with pytest.raises(SystemExit, match="requires at least one positional line or --file"):
        run(["ticket", str(t), "log"])


def test_remember_requires_input(tickets_root, capsys):
    t = make_ticket()
    capsys.readouterr()
    with pytest.raises(SystemExit, match="remember requires at least one positional line or --file"):
        run(["ticket", str(t), "remember"])


def test_silent_format_is_removed(tickets_root, capsys):
    t = make_ticket()
    with pytest.raises(SystemExit) as excinfo:
        run(["ticket", str(t), "log", "old quiet write", "--format", "silent"])
    assert excinfo.value.code == 2
    assert "invalid choice: 'silent'" in capsys.readouterr().err

    with pytest.raises(SystemExit) as excinfo:
        run([
            "message", "send",
            "--ticket", str(t),
            "old quiet notice",
            *owner_args(),
            "--format", "silent",
        ])
    assert excinfo.value.code == 2
    assert "invalid choice: 'silent'" in capsys.readouterr().err


def test_removed_payload_options_are_invalid(tickets_root, capsys):
    t = make_ticket()
    with pytest.raises(SystemExit) as exc:
        run(["ticket", str(t), "log", "--line", "old log"])
    assert exc.value.code == 2
    assert "unrecognized arguments: --line" in capsys.readouterr().err

    with pytest.raises(SystemExit) as exc:
        run(["ticket", str(t), "context", "--text", "old summary"])
    assert exc.value.code == 2
    assert "unrecognized arguments: --text" in capsys.readouterr().err

    with pytest.raises(SystemExit) as exc:
        run(["ticket", str(t), "add-item", "--uri", "https://x/old"])
    assert exc.value.code == 2
    assert "unrecognized arguments: --uri" in capsys.readouterr().err

    with pytest.raises(SystemExit) as exc:
        run(["message", "send", "--ticket", str(t), "--message", "old notice", *owner_args()])
    assert exc.value.code == 2
    assert "unrecognized arguments: --message" in capsys.readouterr().err

    with pytest.raises(SystemExit) as exc:
        run(["ticket", str(t), "log", "--line=old log"])
    assert "append payload must not start with option syntax" in str(exc.value)


def test_positional_newlines_escape_to_single_rendered_line(tickets_root):
    t = make_ticket()
    assert run(["ticket", str(t), "log", "line one\nline two"]) == 0
    assert run([
        "message", "send",
        "--ticket", str(t),
        "notice one\nnotice two",
        *owner_args(),
    ]) == 0

    md = (Path(t) / "TICKET.md").read_text(encoding="utf-8")
    assert "line one\\nline two" in md
    assert "notice one\\nnotice two" in md
    assert "line one\nline two" not in md
    assert "notice one\nnotice two" not in md


def test_add_item_new_duplicate_and_recent_preview(tickets_root, capsys):
    t = make_ticket()
    run(["ticket", str(t), "add-item", "https://x/1"])
    capsys.readouterr()
    run(["ticket", str(t), "add-item", "https://x/1"])
    out = _capture(capsys)
    assert "added 0" in out
    assert "was already present: true" in out

    for i in range(2, 9):
        run(["ticket", str(t), "add-item", f"https://x/{i}"])
        if i < 8:
            capsys.readouterr()
    out = _capture(capsys)
    assert "recent items" in out
    assert "https://x/8" in out
    assert "https://x/1" not in out


def test_json_format_for_change_goal_and_add_item(tickets_root, capsys):
    t = make_ticket()
    capsys.readouterr()
    run(["ticket", str(t), "goal", "json goal", "--format", "json"])
    payload = json.loads(_capture(capsys))
    assert payload["change"]["changed_field"] == "goal"
    assert payload["snapshot"]["goal"] == "json goal"

    run(["ticket", str(t), "add-item", "https://x/1", "--format", "json"])
    payload = json.loads(_capture(capsys))
    assert payload["change"]["appended_field"] == "items"
    assert payload["snapshot"]["items"] == ["https://x/1"]


def test_json_format_includes_unread_messages_without_extra_warning(tickets_root, capsys):
    t = make_ticket()
    run([
        "message", "send",
        "--ticket", str(t),
        "json caller should see this",
        *owner_args(),
    ])
    capsys.readouterr()

    run(["ticket", str(t), "goal", "json notices", "--format", "json"])
    cap = capsys.readouterr()
    payload = json.loads(cap.err)
    assert payload["unread_messages"]["count"] == 1
    assert payload["unread_messages"]["messages"][0]["message"] == "json caller should see this"
    assert "message: ticket has" not in cap.err


def test_json_unread_messages_are_added_by_cli_aspect(tickets_root, capsys):
    t = make_ticket()
    run([
        "message", "send",
        "--ticket", str(t),
        "aspect should attach this",
        *owner_args(),
    ])
    capsys.readouterr()

    run(["ticket", str(t), "goal", "json aspect goal", "--format", "json"])
    cap = capsys.readouterr()
    payload = json.loads(cap.err)

    assert payload["change"]["changed_field"] == "goal"
    assert payload["snapshot"]["goal"] == "json aspect goal"
    assert payload["unread_messages"]["ticket"] == str(Path(t).resolve())
    assert payload["unread_messages"]["messages"][0]["message"] == "aspect should attach this"


def test_json_unread_notice_aspect_is_best_effort_when_db_disappears(tickets_root):
    t = make_ticket()
    shutil.rmtree(Path(t) / "state")
    original = '{\n  "ticket": "demo",\n  "change": {}\n}\n'

    assert attach_unread_messages_to_json(original, str(t)) == original


def test_fork_stdout_is_path_only(tickets_root, capsys):
    src = make_ticket("src-for-fork")
    capsys.readouterr()
    run(["ticket", str(src), "fork", "--topic", "child-fork", "--goal", "child goal", *owner_args()])
    cap = capsys.readouterr()
    stdout_lines = cap.out.splitlines()
    assert len(stdout_lines) == 1
    assert Path(stdout_lines[0]).is_dir()
    assert "ticket: " in cap.err
