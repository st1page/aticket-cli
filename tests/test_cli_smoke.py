"""End-to-end CLI smoke: the full lifecycle the README documents, in-process."""
import pytest

from conftest import make_ticket, run
from ticket_cli.cli import build_parser


def test_public_help_has_no_standalone_canonical_source_commands(capsys):
    top_help = build_parser().format_help()
    assert "usage: aticket-cli <resource>" in top_help
    assert "      aticket-cli ticket new" in top_help
    assert "      aticket-cli tickets search --query <keywords>" in top_help
    assert "backfill-created-at" not in top_help
    for removed in ("render", "check-fresh", "ref", "close-fork", "list", "items"):
        assert f"      aticket-cli {removed}" not in top_help
    assert "switch-canonical" not in top_help
    assert "set " not in top_help

    with pytest.raises(SystemExit) as exc:
        run(["set", "--help"])
    assert exc.value.code == 2
    cap = capsys.readouterr()
    assert "invalid choice" in cap.err


def test_switch_canonical_is_not_a_public_command(capsys):
    with pytest.raises(SystemExit) as exc:
        run(["switch-canonical", "--help"])
    assert exc.value.code == 2
    cap = capsys.readouterr()
    assert "invalid choice" in cap.err


@pytest.mark.parametrize(
    "verb",
    [
        "new", "claim", "release", "fork", "archive", "change-goal",
        "short-context", "log", "artifact", "add-item", "search",
        "render", "check-fresh", "ref", "close-fork", "list", "items",
    ],
)
def test_removed_utility_commands_are_not_public(verb, capsys):
    with pytest.raises(SystemExit) as exc:
        run([verb, "--help"])
    assert exc.value.code == 2
    cap = capsys.readouterr()
    assert "invalid choice" in cap.err


def test_old_message_checked_shape_is_invalid(capsys):
    with pytest.raises(SystemExit) as exc:
        run(["message", "checked", "--ticket", "/tmp/t", "--until-id", "1"])
    assert exc.value.code == 2
    assert "invalid choice: 'checked'" in capsys.readouterr().err


def test_old_notice_resource_is_invalid(capsys):
    with pytest.raises(SystemExit) as exc:
        run(["notice", "send", "--ticket", "/tmp/t", "old shape"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "invalid choice: 'notice'" in err
    assert "message send" not in err


def test_old_ticket_notice_checked_shape_is_invalid(capsys):
    with pytest.raises(SystemExit) as exc:
        run(["ticket", "/tmp/example-ticket", "notice", "checked", "--until-id", "1"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "unknown ticket action: notice" in err
    assert "notice checked" not in err


def test_message_send_help_shows_required_message(capsys):
    with pytest.raises(SystemExit) as exc:
        run(["message", "send", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "MESSAGE" in out
    assert "[MESSAGE]" not in out


def test_resource_help_and_errors_are_agent_discoverable(capsys):
    with pytest.raises(SystemExit) as exc:
        run(["ticket", "--help"])
    assert exc.value.code == 0
    cap = capsys.readouterr()
    assert "aticket-cli ticket new --topic" in cap.out
    assert "aticket-cli ticket <ticket-dir> message checked" in cap.out
    assert "aticket-cli ticket <ticket-dir> brief" in cap.out
    assert "aticket-cli ticket <ticket-dir> artifact" not in cap.out
    assert "ticket_args" not in cap.out

    with pytest.raises(SystemExit) as exc:
        run(["tickets", "--help"])
    assert exc.value.code == 0
    cap = capsys.readouterr()
    assert "aticket-cli tickets search --query" in cap.out
    assert "aticket-cli tickets search --reindex" in cap.out
    assert "backfill-created-at" not in cap.out
    assert "tickets_args" not in cap.out

    with pytest.raises(SystemExit) as exc:
        run(["ticket", "/tmp/example-ticket", "unknown"])
    assert exc.value.code == 2
    cap = capsys.readouterr()
    assert "unknown ticket action: unknown" in cap.err
    assert "actions: claim, release, fork" in cap.err
    assert "brief" in cap.err

    with pytest.raises(SystemExit) as exc:
        run(["tickets", "unknown"])
    assert exc.value.code == 2
    cap = capsys.readouterr()
    assert "unknown tickets action: unknown" in cap.err
    assert "actions: search" in cap.err

    with pytest.raises(SystemExit) as exc:
        run(["tickets", "backfill-created-at"])
    assert exc.value.code == 2
    cap = capsys.readouterr()
    assert "unknown tickets action: backfill-created-at" in cap.err
    assert "actions: search, squash" in cap.err

    with pytest.raises(SystemExit) as exc:
        run(["ticket", "/tmp/example-ticket", "--help"])
    assert exc.value.code == 0
    cap = capsys.readouterr()
    assert "usage: aticket-cli ticket /tmp/example-ticket <action>" in cap.out
    assert "actions: claim, release, fork" in cap.out
    assert "brief" in cap.out
    assert "artifact" not in cap.out

    with pytest.raises(SystemExit) as exc:
        run(["ticket", "/tmp/example-ticket", "message", "--help"])
    assert exc.value.code == 0
    cap = capsys.readouterr()
    assert "usage: aticket-cli ticket /tmp/example-ticket message checked" in cap.out
    assert "actions: checked" in cap.out


def test_ticket_artifact_action_is_removed(capsys):
    with pytest.raises(SystemExit) as exc:
        run(["ticket", "/tmp/example-ticket", "artifact", "out.csv"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "unknown ticket action: artifact" in err
    assert "actions: claim, release, fork" in err


def test_full_flow(tickets_root, capsys):
    # create
    t = make_ticket("smoke")
    capsys.readouterr()

    # mutate
    assert run(["ticket", str(t), "goal", "prove it"]) == 0
    assert run(["ticket", str(t), "context", "ready to inspect"]) == 0
    assert run(["ticket", str(t), "log", "started"]) == 0
    assert run(["ticket", str(t), "add-item", "file:///tmp/out.csv"]) == 0

    md = t / "TICKET.md"
    assert "prove it" in md.read_text(encoding="utf-8")
