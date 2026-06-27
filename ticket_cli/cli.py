"""Single `aticket-cli` entrypoint.

The public CLI is resource-oriented:

    aticket-cli ticket new --topic ... --goal ...
    aticket-cli ticket <ticket-dir> log "..."
    aticket-cli tickets search --query "..."
    aticket-cli message send --ticket <ticket-dir> "..."

The implementation still reuses the small handler modules (`ticket_new`,
`lifecycle`, `fork`, `notice`, `search_index`) by translating the resource-style
surface into the argparse namespaces those handlers expect."""
from __future__ import annotations

import argparse
import contextlib
import io
import sys

from .config import ensure_config_file_for_command
from . import identity, lifecycle, ticket_new
from .notice_alert import attach_unread_messages_to_json, emit_unread_message_warning
from .reminders import emit_must_remember_reminder


# ---- Agent workflow stage grouping -----------------------------------------
#
# Each entry: (stage_label, one-line stage description, [command examples, ...]).
COMMAND_GROUPS: list[tuple[str, str, list[str]]] = [
    (
        "Discover",
        "Find work that already exists (use this even when you're already inside a ticket — other tickets are also an information source)",
        ["tickets search --query <keywords>"],
    ),
    (
        "Claim",
        "Start a new ticket, take over an existing one, or fork off a derived ticket",
        ["ticket new", "ticket <ticket-dir> claim", "ticket <ticket-dir> release", "ticket <ticket-dir> fork"],
    ),
    (
        "Work",
        "Record progress, decisions, URI items, and concise recovery context on a held ticket",
        [
            "ticket <ticket-dir> log",
            "ticket <ticket-dir> remember",
            "ticket <ticket-dir> forget",
            "ticket <ticket-dir> brief",
            "ticket <ticket-dir> goal",
            "ticket <ticket-dir> context",
            "ticket <ticket-dir> add-item",
            "message send",
            "ticket <ticket-dir> message checked",
        ],
    ),
    (
        "Handoff & Archive",
        "Make the ticket discoverable from PRs / docs / next agent, then archive it when complete",
        [
            "tickets squash <archived-ticket> <archived-ticket> --topic <topic> --goal <goal>",
            "ticket <ticket-dir> archive",
        ],
    ),
]

_TICKET_RESOURCE_USAGE = "usage: aticket-cli ticket new ... OR aticket-cli ticket <ticket-dir> <action> ..."
_TICKET_ACTIONS = "actions: claim, release, fork, archive, goal, context, remember, forget, brief, log, add-item, message checked"
_TICKETS_RESOURCE_USAGE = "usage: aticket-cli tickets search ... OR aticket-cli tickets squash ..."
_TICKETS_ACTIONS = "actions: search, squash"


def _format_grouped_help(parser: argparse.ArgumentParser) -> str:
    """Render top-level help grouped by agent workflow stage.

    We hand-roll the usage line (instead of letting argparse format it from the
    subparsers choices) so the help can show a clean `aticket-cli <resource> ...`
    shape without setting `parser.usage`. Setting `parser.usage` on
    the root would leak into every subparser's `prog` and produce broken
    action help lines. Keeping the override here, in our own help formatter, isolates
    the cosmetic change to the root help and leaves subparsers unaffected.
    """
    lines: list[str] = [
        "usage: aticket-cli <resource> [<resource-args>] <action> [options]",
        "",
        parser.description or "",
        "",
        "workflow (by what you're doing right now):",
        "",
    ]
    for label, desc, verbs in COMMAND_GROUPS:
        lines.append(f"  {label}")
        lines.append(f"    {desc}")
        for command in verbs:
            lines.append(f"      aticket-cli {command}")
        lines.append("")

    # Footer: options block (e.g. -h).
    opt_formatter = parser._get_formatter()
    optional_group = next(
        (g for g in parser._action_groups if g.title in ("options", "optional arguments")),
        None,
    )
    if optional_group and optional_group._group_actions:
        opt_formatter.start_section(optional_group.title)
        opt_formatter.add_arguments(optional_group._group_actions)
        opt_formatter.end_section()
        lines.append(opt_formatter.format_help().rstrip())
        lines.append("")

    lines.append(
        "Pick the stage that matches what you're doing now, then "
        "`aticket-cli <resource> --help` for details."
    )
    return "\n".join(lines).rstrip() + "\n"


class _GroupedHelpParser(argparse.ArgumentParser):
    """Root parser whose --help is grouped by workflow stage instead of a flat list."""

    def format_help(self) -> str:  # noqa: D401 — argparse override
        return _format_grouped_help(self)


def _render_by_default(p: argparse.ArgumentParser) -> None:
    p.set_defaults(no_render=False)


def _add_write_format(p: argparse.ArgumentParser) -> None:
    """Output format for the post-write snapshot. plain (default) is the
    human/agent-readable text block; json is machine-parseable."""
    p.add_argument(
        "--format",
        default="plain",
        choices=("plain", "json"),
        help="Output format for the post-write snapshot (default: plain).",
    )


def _add_owner_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--agent-type", choices=("codex", "claude"), help=identity.AGENT_TYPE_HELP)
    p.add_argument("--session-id", help=identity.SESSION_ID_HELP)
    p.add_argument("--owner-label", default="", help="Human-readable lease holder label")


def _add_new_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--topic", required=True, help="Topic for the new ticket")
    p.add_argument("--goal", required=True, help="Non-empty goal for this ticket")
    p.add_argument("--short-context", default="", help="Concise recovery context for current state and next move")
    p.add_argument(
        "--backlog",
        action="store_true",
        help="Create the ticket unclaimed with lifecycle BACKLOG instead of immediately claiming it",
    )
    _add_owner_args(p)
    _add_write_format(p)


def _add_claim_args(p: argparse.ArgumentParser) -> None:
    _add_owner_args(p)
    p.add_argument("--force", action="store_true", help="Take over even if another holder is active")
    p.add_argument(
        "--confirm-human-approved-takeover",
        action="store_true",
        help="Required with --force after asking a human to approve takeover from a different active holder",
    )
    _add_write_format(p)


def _add_release_args(p: argparse.ArgumentParser) -> None:
    _add_owner_args(p)
    p.add_argument("--force", action="store_true", help="Release even if the inferred holder differs")
    _add_write_format(p)


def _add_replace_payload_args(p: argparse.ArgumentParser, *, metavar: str, text_help: str) -> None:
    p.add_argument("payload", nargs="?", metavar=metavar, help=text_help)
    p.add_argument("--file", default="", help="Read replacement text from file")


def _add_append_payload_args(p: argparse.ArgumentParser) -> None:
    """Shared positional payload + `--file` payload for append commands."""
    p.add_argument(
        "payload",
        nargs="*",
        metavar="LINE",
        help=(
            "Bullet text to append. Repeat the positional argument to append "
            "multiple bullets; embedded newlines are escaped so each argument "
            "renders as one bullet line."
        ),
    )
    p.add_argument(
        "--file",
        default="",
        help="File whose non-empty lines become bullets (appended after positional values).",
    )


def _add_add_item_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "uri",
        nargs="?",
        metavar="URI",
        help=(
            "Item URI to append if absent "
            "(e.g. file:///abs/path, https://host/page, agents://codex/thread)"
        ),
    )
    _render_by_default(p); _add_write_format(p)


def _add_archive_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--agent-confirm-archive-large-dir",
        action="store_true",
        help="Required to archive a ticket directory above the configured agent confirmation threshold.",
    )
    p.add_argument(
        "--agnet-confirm-archive-large-dir",
        dest="agent_confirm_archive_large_dir",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    p.add_argument(
        "--human-confirm-archive-large-dir",
        action="store_true",
        help="Required after human approval to archive a ticket directory above the configured human confirmation threshold.",
    )
    _add_write_format(p)


def _invoke_ticket_func(args: argparse.Namespace) -> int:
    """Run a handler for a known ticket and preserve unread-message aspect behavior."""
    func = getattr(args, "func", None)
    if func is None:
        raise SystemExit("internal error: ticket command missing handler")
    ticket = getattr(args, "ticket", "")
    fmt = getattr(args, "format", "plain")
    if ticket and fmt == "json":
        stderr_buf = io.StringIO()
        try:
            with contextlib.redirect_stderr(stderr_buf):
                rc = func(args)
        except BaseException:
            sys.stderr.write(stderr_buf.getvalue())
            raise
        stderr_text = stderr_buf.getvalue()
        if rc == 0:
            stderr_text = attach_unread_messages_to_json(stderr_text, ticket)
        sys.stderr.write(stderr_text)
        return rc
    rc = func(args)
    if rc == 0 and ticket:
        if not getattr(args, "suppress_must_remember_aspect", False):
            emit_must_remember_reminder(ticket, fmt=fmt)
        emit_unread_message_warning(ticket, fmt=fmt)
    return rc


def _parser(prog: str, description: str = "") -> argparse.ArgumentParser:
    return argparse.ArgumentParser(prog=prog, description=description or None)


def _resource_error(message: str, usage: str, actions: str = "") -> None:
    sys.stderr.write(f"{usage}\n")
    if actions:
        sys.stderr.write(f"{actions}\n")
    sys.stderr.write(f"error: {message}\n")
    raise SystemExit(2)


def _print_ticket_action_help(ticket: str) -> None:
    print(f"usage: aticket-cli ticket {ticket} <action> [options]")
    print()
    print(_TICKET_ACTIONS)
    print()
    print("Examples:")
    print(f"  aticket-cli ticket {ticket} claim")
    print(f"  aticket-cli ticket {ticket} release")
    print(f"  aticket-cli ticket {ticket} fork --topic child --goal \"child goal\"")
    print(f"  aticket-cli ticket {ticket} goal \"new goal\"")
    print(f"  aticket-cli ticket {ticket} context \"state and next step\"")
    print(f"  aticket-cli ticket {ticket} remember \"principle/preflight/invariant that must survive handoff\"")
    print(f"  aticket-cli ticket {ticket} forget 2")
    print(f"  aticket-cli ticket {ticket} brief")
    print(f"  aticket-cli ticket {ticket} log \"what happened\"")
    print(f"  aticket-cli ticket {ticket} add-item \"https://example.test/pr/1\"")
    print(f"  aticket-cli ticket {ticket} add-item \"file:///abs/path\"")
    print(f"  aticket-cli ticket {ticket} message checked --until-id 3")
    print(f"  aticket-cli ticket {ticket} archive")
    raise SystemExit(0)


def _print_ticket_message_help(ticket: str) -> None:
    print(f"usage: aticket-cli ticket {ticket} message checked --until-id <id>")
    print()
    print("actions: checked")
    print()
    print("Examples:")
    print(f"  aticket-cli ticket {ticket} message checked --until-id 3")
    raise SystemExit(0)


def _parse_ticket_new(rest: list[str]) -> argparse.Namespace:
    p = _parser("aticket-cli ticket new", "Create a new ticket, claimed by default or unclaimed with --backlog")
    _add_new_args(p)
    args = p.parse_args(rest)
    args.func = ticket_new.cmd_new
    return args


def _parse_ticket_action(ticket: str, action: str, rest: list[str]) -> argparse.Namespace:
    prog = f"aticket-cli ticket {ticket} {action}"
    if action in ("-h", "--help"):
        _print_ticket_action_help(ticket)
    if action == "claim":
        p = _parser(prog, "Claim an existing ticket's workspace/current-progress lease")
        _add_claim_args(p)
        args = p.parse_args(rest)
        args.func = ticket_new.cmd_claim
    elif action == "release":
        p = _parser(prog, "Release the current ticket lease")
        _add_release_args(p)
        args = p.parse_args(rest)
        args.func = ticket_new.cmd_release
    elif action == "fork":
        from . import fork as _fork

        p = _parser(prog, "Fork this ticket into a new independent ticket")
        p.add_argument("--topic", required=True, help="New forked-ticket topic")
        p.add_argument("--goal", required=True, help="Non-empty goal for the forked ticket")
        _add_owner_args(p)
        p.add_argument("--copy-path", action="append", default=[])
        _add_write_format(p)
        args = p.parse_args(rest)
        args.func = _fork.cmd_fork
    elif action == "archive":
        p = _parser(prog, "Archive this ticket; archived tickets cannot be modified")
        _add_archive_args(p)
        args = p.parse_args(rest)
        args.func = lifecycle.cmd_archive
    elif action == "brief":
        from . import brief as _brief

        p = _parser(prog, "Print a recovery/preflight brief for this ticket")
        p.add_argument("--format", default="plain", choices=("plain", "json"))
        args = p.parse_args(rest)
        args.func = _brief.cmd_brief
        args.suppress_must_remember_aspect = True
    elif action == "goal":
        p = _parser(prog, "Replace this ticket's goal")
        _add_replace_payload_args(p, metavar="GOAL", text_help="New non-empty goal")
        _render_by_default(p); _add_write_format(p)
        args = p.parse_args(rest)
        args.func = lifecycle.cmd_change_goal
    elif action == "context":
        p = _parser(prog, "Replace this ticket's concise recovery context")
        _add_replace_payload_args(
            p,
            metavar="SUMMARY",
            text_help="Concise current-state / next-action / useful-note summary",
        )
        _render_by_default(p); _add_write_format(p)
        args = p.parse_args(rest)
        args.func = lifecycle.cmd_short_context
    elif action == "log":
        p = _parser(prog, "Append a Work log entry (timestamped)")
        _add_append_payload_args(p); _render_by_default(p); _add_write_format(p)
        args = p.parse_args(rest)
        args.func = lifecycle.cmd_append_work_log
    elif action == "remember":
        p = _parser(prog, "Append Must remember entries inherited by forked tickets (max 16)")
        _add_append_payload_args(p); _render_by_default(p); _add_write_format(p)
        args = p.parse_args(rest)
        args.func = lifecycle.cmd_append_must_remember
    elif action == "forget":
        p = _parser(prog, "Delete one Must remember entry by 1-based index")
        p.add_argument("index", nargs="?", metavar="INDEX", help="1-based Must remember entry index to delete")
        _render_by_default(p); _add_write_format(p)
        args = p.parse_args(rest)
        args.func = lifecycle.cmd_forget_must_remember
    elif action == "add-item":
        p = _parser(prog, "Append one URI item without replacing existing items")
        _add_add_item_args(p)
        args = p.parse_args(rest)
        args.func = lifecycle.cmd_append_item
    elif action == "message":
        if not rest:
            _resource_error(
                "missing ticket message action",
                "usage: aticket-cli ticket <ticket-dir> message checked --until-id <id>",
                "actions: checked",
            )
        message_action, message_rest = rest[0], rest[1:]
        if message_action in ("-h", "--help"):
            _print_ticket_message_help(ticket)
        if message_action != "checked":
            _resource_error(
                f"unknown ticket message action: {message_action}",
                "usage: aticket-cli ticket <ticket-dir> message checked --until-id <id>",
                "actions: checked",
            )
        from . import notice as _notice

        p = _parser(f"{prog} checked", "Mark messages up to an id as checked")
        p.add_argument("--until-id", required=True, type=int, help="Mark messages with id <= this value as checked")
        _add_owner_args(p)
        _add_write_format(p)
        args = p.parse_args(message_rest)
        args.func = _notice.cmd_message_checked
    else:
        _resource_error(f"unknown ticket action: {action}", _TICKET_RESOURCE_USAGE, _TICKET_ACTIONS)
    args.ticket = ticket
    return args


def cmd_ticket_resource(args: argparse.Namespace) -> int:
    tokens = list(getattr(args, "ticket_args", []) or [])
    if not tokens:
        _resource_error("missing ticket action", _TICKET_RESOURCE_USAGE, _TICKET_ACTIONS)
    if tokens[0] == "new":
        parsed = _parse_ticket_new(tokens[1:])
        ensure_config_file_for_command()
        return parsed.func(parsed)
    if len(tokens) < 2:
        _resource_error("missing ticket action", _TICKET_RESOURCE_USAGE, _TICKET_ACTIONS)
    parsed = _parse_ticket_action(tokens[0], tokens[1], tokens[2:])
    ensure_config_file_for_command()
    return _invoke_ticket_func(parsed)


def _parse_tickets_search(rest: list[str]) -> argparse.Namespace:
    from . import search_index

    p = _parser("aticket-cli tickets search", "Global keyword search across tickets")
    query_mode = p.add_mutually_exclusive_group(required=True)
    query_mode.add_argument("--query", default="", help="Keyword query")
    query_mode.add_argument("--reindex", action="store_true", help="Rebuild the derived global search index")
    p.add_argument("--kind", default="all", choices=("all", "ticket"))
    p.add_argument("--lifecycle-state", default="ALL", choices=("BACKLOG", "ACTIVE", "ARCHIVED", "ALL"))
    p.add_argument(
        "--ticket",
        action="append",
        default=[],
        help="Restrict --query to a ticket directory or ticket basename; can be repeated",
    )
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--format", default="tsv", choices=("tsv", "json"))
    args = p.parse_args(rest)
    args.func = search_index.cmd_search
    return args


def _parse_tickets_squash(rest: list[str]) -> argparse.Namespace:
    from . import squash

    p = _parser("aticket-cli tickets squash", "Squash archived tickets into a continuing larger ticket")
    p.add_argument("source_tickets", nargs="+", help="Archived source ticket directories to squash")
    p.add_argument("--topic", required=True, help="Topic for the new squash ticket")
    p.add_argument("--goal", required=True, help="Non-empty goal for the new squash ticket")
    p.add_argument("--short-context", default="", help="Explicit recovery context for the new squash ticket")
    p.add_argument("--summary", default="", help="Short human summary of what the source tickets collectively did")
    p.add_argument("--summary-file", default="", help="Read squash summary from file")
    p.add_argument("--next", dest="next_step", default="", help="Next step for continuing work on the squash ticket")
    p.add_argument("--next-file", default="", help="Read next-step text from file")
    state = p.add_mutually_exclusive_group()
    state.add_argument("--backlog", action="store_true", help="Create the squash target unclaimed with lifecycle BACKLOG")
    state.add_argument("--archive", action="store_true", help="Create the squash target already ARCHIVED")
    _add_owner_args(p)
    _add_write_format(p)
    args = p.parse_args(rest)
    args.func = squash.cmd_squash
    return args


def cmd_tickets_resource(args: argparse.Namespace) -> int:
    tokens = list(getattr(args, "tickets_args", []) or [])
    if not tokens:
        _resource_error("missing tickets action", _TICKETS_RESOURCE_USAGE, _TICKETS_ACTIONS)
    action, rest = tokens[0], tokens[1:]
    if action == "search":
        parsed = _parse_tickets_search(rest)
    elif action == "squash":
        parsed = _parse_tickets_squash(rest)
    else:
        _resource_error(f"unknown tickets action: {action}", _TICKETS_RESOURCE_USAGE, _TICKETS_ACTIONS)
    ensure_config_file_for_command()
    return parsed.func(parsed)


def _register_message_send(sub) -> None:
    from . import notice as _notice

    p = sub.add_parser("message", help="External message actions")
    message_sub = p.add_subparsers(dest="message_command", required=True)
    send = message_sub.add_parser("send", help="Append an external message to a ticket")
    send.add_argument("--ticket", required=True, help="Target ticket directory")
    send.add_argument("message", metavar="MESSAGE", help="Single-line message summary")
    send.add_argument("--from-ticket", default="", help="Optional source ticket directory")
    send.add_argument(
        "--with",
        dest="with_items",
        action="append",
        default=[],
        metavar="URI",
        help="Related single-line URI item (repeatable)",
    )
    send.add_argument(
        "--allow-archived",
        action="store_true",
        help="Allow appending historical context to an archived ticket; no active delivery is guaranteed",
    )
    _add_owner_args(send)
    _add_write_format(send)
    send.set_defaults(func=_notice.cmd_message_send, suppress_unread_message_aspect=True)


def build_parser() -> argparse.ArgumentParser:
    ap = _GroupedHelpParser(
        prog="aticket-cli",
        description="Consolidated CLI for agent work tickets.",
    )
    # parser_class=argparse.ArgumentParser keeps the grouped --help override
    # confined to the root parser; subcommand parsers use the default class so
    # their own --help renders normally instead of inheriting the grouped view.
    sub = ap.add_subparsers(dest="resource", required=True, parser_class=argparse.ArgumentParser)

    p = sub.add_parser(
        "ticket",
        help="Create or operate on one ticket",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        usage="aticket-cli ticket new ... | aticket-cli ticket <ticket-dir> <action> ...",
        description=(
            "Create or operate on one ticket.\n\n"
            "Examples:\n"
            "  aticket-cli ticket new --topic demo --goal \"prove the flow\"\n"
            "  aticket-cli ticket new --topic follow-up --goal \"later work\" --backlog\n"
            "  aticket-cli ticket <ticket-dir> claim\n"
            "  aticket-cli ticket <ticket-dir> release\n"
            "  aticket-cli ticket <ticket-dir> fork --topic child --goal \"child goal\"\n"
            "  aticket-cli ticket <ticket-dir> goal \"new goal\"\n"
            "  aticket-cli ticket <ticket-dir> context \"state and next step\"\n"
            "  aticket-cli ticket <ticket-dir> remember \"principle/preflight/invariant to keep visible\"\n"
            "  aticket-cli ticket <ticket-dir> forget 2\n"
            "  aticket-cli ticket <ticket-dir> brief\n"
            "  aticket-cli ticket <ticket-dir> log \"what happened\"\n"
            "  aticket-cli ticket <ticket-dir> add-item \"https://example.test/pr/1\"\n"
            "  aticket-cli ticket <ticket-dir> add-item \"file:///abs/path\"\n"
            "  aticket-cli ticket <ticket-dir> message checked --until-id 3\n"
            "  aticket-cli ticket <ticket-dir> archive"
        ),
    )
    p.add_argument("ticket_args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_ticket_resource)

    p = sub.add_parser(
        "tickets",
        help="Query the ticket collection",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        usage="aticket-cli tickets search ... | aticket-cli tickets squash ...",
        description=(
            "Query the ticket collection.\n\n"
            "Examples:\n"
            "  aticket-cli tickets search --query \"keyword\"\n"
            "  aticket-cli tickets search --query \"keyword\" --kind ticket\n"
            "  aticket-cli tickets search --query \"keyword\" --lifecycle-state BACKLOG\n"
            "  aticket-cli tickets search --query \"keyword\" --lifecycle-state ACTIVE\n"
            "  aticket-cli tickets search --query \"keyword\" --ticket <ticket-dir>\n"
            "  aticket-cli tickets search --reindex\n"
            "  aticket-cli tickets squash <archived-ticket-a> <archived-ticket-b> --topic combined --goal \"continue combined work\""
        ),
    )
    p.add_argument("tickets_args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_tickets_resource)

    _register_message_send(sub)

    return ap


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 2
    if getattr(args, "resource", "") not in ("ticket", "tickets"):
        ensure_config_file_for_command()
    ticket = getattr(args, "ticket", "")
    fmt = getattr(args, "format", "plain")
    suppress_message_aspect = bool(getattr(args, "suppress_unread_message_aspect", False))
    if ticket and fmt == "json" and not suppress_message_aspect:
        stderr_buf = io.StringIO()
        try:
            with contextlib.redirect_stderr(stderr_buf):
                rc = func(args)
        except BaseException:
            sys.stderr.write(stderr_buf.getvalue())
            raise
        stderr_text = stderr_buf.getvalue()
        if rc == 0:
            stderr_text = attach_unread_messages_to_json(stderr_text, ticket)
        sys.stderr.write(stderr_text)
        return rc
    rc = func(args)
    if rc == 0 and ticket and not suppress_message_aspect:
        emit_unread_message_warning(ticket, fmt=fmt)
    return rc
