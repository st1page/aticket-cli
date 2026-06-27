# aticket-cli

`aticket-cli` is intended to be used together with
[`stone-harness`](https://github.com/st1page/stone-harness): this repository
provides the ticket/workspace CLI, while `stone-harness` provides an example
agent control-plane harness that depends on it.

`aticket-cli` is a single binary-style CLI for agent **work tickets**. It
consolidates what used to be scattered shell/python helpers into one
agent-facing entrypoint, with `ticket` kept as the work-unit noun.

A *ticket* is one unit of work: a directory holding a rendered `TICKET.md`, a
sqlite truth source, plus `notes/ artifacts/ workspace/`.

```
<ticket-dir>/
├── TICKET.md          # rendered view (managed-by: sqlite marker on line 1)
├── state/             # ticket.sqlite3 (+ fork.json)
├── notes/             # process notes and handoff notes
├── artifacts/         # immutable evidence files, snapshots, run outputs, attachments
└── workspace/         # scratch / cross-repo worktrees (was tmp/)
```

Tickets live under `AGENT_TICKETS_ROOT` or the configured ticket root
(default `/code/tsshi/agent-tickets`), in `tickets/`.

## Install / run

Zero install — just run the entrypoint with Python 3.12+:

```bash
/code/tsshi/aticket-cli/aticket-cli --help
```

Or install to get an `aticket-cli` command on PATH:

```bash
pip install -e /code/tsshi/aticket-cli   # provides `aticket-cli`
```

## Commands

| Command | What it does |
|---|---|
| `aticket-cli ticket new --topic <t> --goal <goal>` | Create and claim a ticket; internally stores a verified `agents://...` owner xurl inferred from the current agent |
| `aticket-cli ticket new --topic <t> --goal <goal> --backlog` | Create an unclaimed `BACKLOG` ticket for later work |
| `aticket-cli ticket <dir> claim\|release` | Claim or release the ticket lease with a locally verified agent session; claim moves `BACKLOG -> ACTIVE`, release moves `ACTIVE -> BACKLOG` |
| `aticket-cli ticket <dir> goal ...` | Replace the required goal |
| `aticket-cli ticket <dir> context ...` | Replace the concise recovery context |
| `aticket-cli ticket <dir> remember ...` | Append one or more must-remember list entries; max 16 entries per ticket, and forked tickets inherit them |
| `aticket-cli ticket <dir> forget <index>` | Delete one must-remember entry by 1-based index |
| `aticket-cli ticket <dir> brief` | Print the recovery/preflight brief: goal, short context, must-remember list, unread messages, and counts |
| `aticket-cli ticket <dir> log ...` | Append a work-log bullet |
| `aticket-cli ticket <dir> add-item <URI>` | Append one item URI without replacing the current list |
| `aticket-cli message send --ticket <dir> ... [--from-ticket <dir>] [--with <URI> ...] [--allow-archived]` | Append an external message to a ticket |
| `aticket-cli ticket <dir> message checked --until-id <N>` | Mark active messages up to an id as checked and log them |
| `aticket-cli tickets search --query <q> [--kind all\|ticket] [--ticket <dir>]` | Search a derived global FTS index across tickets, optionally scoped to direct tickets |
| `aticket-cli tickets search --reindex` | Rebuild the derived global FTS index from ticket truth sources |
| `aticket-cli tickets squash <archived-a> <archived-b> --topic <t> --goal <goal>` | Squash archived small tickets into a larger continuing ticket and write reverse references into the sources |
| `aticket-cli ticket <src> fork --topic <t> --goal <goal>` | Fork into a new independent ticket with a source snapshot |
| `aticket-cli ticket <dir> archive` | Archive a ticket; archived tickets cannot be modified. Large directories require confirmation flags. |

## Config

All non-help commands require an aticket config file. By default,
`aticket-cli` reads `~/.config/aticket-cli/config.toml`; the first valid
non-help command bootstraps that default file from the packaged template if it
does not exist yet. When this happens, `aticket-cli` writes a stderr notice
showing the created config directory and file, and asks the user to review and
edit `[tickets].root`.
Help commands do not create or require config.

Set `ATICKET_CONFIG=/path/to/config.toml` to use another file. Explicit config
paths are never created implicitly: if the file is missing or unreadable,
`aticket-cli` fails before running the command. Existing environment variables
still win over config values; for example, `AGENT_TICKETS_ROOT` overrides
`[tickets].root`.

The CLI intentionally does not rely on a Python package post-install hook to
write user config. Modern wheel/pip installs do not provide a reliable,
permission-safe place to mutate the installing user's home directory, so config
bootstrap happens at CLI runtime instead.

```toml
[tickets]
root = "/code/tsshi/agent-tickets"

[archive]
agent_confirm_large_dir_mib = 10
human_confirm_large_dir_mib = 100

# Optional. Only relevant when owner/session verification needs that provider.
[identity]
codex_jsonl_root = "~/.codex/sessions"
claude_jsonl_root = "~/.claude/projects"
```

Only `[tickets].root` needs human review at bootstrap. The `[identity]` paths
are optional and only used later to verify that an inferred or explicit agent
session exists locally. The current provider is still inferred from
`CODEX_THREAD_ID` / `CLAUDE_CODE_SESSION_ID` and, when both are present, the
nearest provider process.

When archiving, a ticket directory above `agent_confirm_large_dir_mib` requires
`--agent-confirm-archive-large-dir`; a ticket directory above
`human_confirm_large_dir_mib` requires `--human-confirm-archive-large-dir`.
These flags are a lightweight closeout pause, not a checklist system. Before
using them, inspect `artifacts/` and `workspace/`, compress raw/detail data that
does not need to stay expanded, and make sure the ticket records the final
result or handoff state. For tickets above the human threshold, ask a human
before keeping the large directory as-is.

## Command shape

This release intentionally has no compatibility shims for the old flat command
surface. The old command shapes are invalid; the public interface is the
resource-style command list above.

`aticket-cli message send --ticket <dir> ...` is the retained external-message
targeting shape: the sender is adding information to another ticket without
entering that ticket's resource context. `aticket-cli tickets search --ticket
<dir>` is a read-only scope filter. Holder-side ticket operations still live
under `ticket <dir>`.

## Quick start

Run this from a Codex/Claude tool subprocess so `aticket-cli ticket new` and `aticket-cli ticket <dir> fork`
can infer a locally resolvable agent session from `CODEX_THREAD_ID` or
`CLAUDE_CODE_SESSION_ID`. From a normal shell, pass `--agent-type <codex|claude>
--session-id <uuid>` for an existing local provider session.

```bash
export AGENT_TICKETS_ROOT="$(mktemp -d)"
T=$(./aticket-cli ticket new --topic demo --goal "prove the ticket flow")
./aticket-cli ticket "$T" add-item "file:///code/tsshi/aticket-cli"
./aticket-cli ticket "$T" add-item "https://github.com/example-org/aticket-cli/pull/3"
./aticket-cli ticket "$T" add-item "https://docs.example.com/agent-ticket-demo"
rg -n 'github|docs|file://' "$T/TICKET.md"
./aticket-cli ticket "$T" log "started"
./aticket-cli ticket "$T" remember "Preflight: verify linked worktree before repo edits"
./aticket-cli ticket "$T" brief
./aticket-cli ticket "$T" context \
  "Demo ticket is initialized; next action is to run the search/fork smoke."
ls "$AGENT_TICKETS_ROOT/tickets"
./aticket-cli tickets search --query prove
F=$(./aticket-cli ticket "$T" fork \
  --topic demo-fork --goal "prove fork snapshot")
./aticket-cli ticket "$T" add-item "file://$F"
./aticket-cli ticket "$T" archive
```

## Rendered TICKET.md example

`TICKET.md` is generated from sqlite by the renderer. This example shows the
rendered shape; it is not a source template.

```markdown
<!-- managed-by: sqlite -->
# Ticket: 2026-06-23-demo-ticket-120000
Lifecycle: ACTIVE
Owner: claim by codex (019ef28a) at 2026-06-23T12:00:00+08:00

## Goal
Prove the ticket flow.

## Short context
Demo ticket is initialized; next action is to run the search/fork smoke.

## Must remember
1. Preflight: verify linked worktree before repo edits.

## Messages

## Scope / Non-goals

## Items
- file:///code/tsshi/aticket-cli
- https://github.com/example-org/aticket-cli/pull/3
- https://docs.example.com/agent-ticket-demo
- agents://codex/019ef28a-0000-0000-0000-000000000000

## Environment

## Work log
- 12:00:00: Claimed ticket by `codex (019ef28a)`.
- 12:01:00: started

## Decisions

## Artifacts

---
Rendered from DB revision: 4
Rendered at: 2026-06-23T12:01:00+08:00
```

To consolidate several small completed tickets under one larger continuing
work unit, archive the source tickets first and then squash them:

```bash
BIG=$(./aticket-cli tickets squash "$SMALL_A" "$SMALL_B" \
  --topic combined-work \
  --goal "Continue the larger work" \
  --summary "These small tickets were one human-level intent" \
  --next "Keep future work in this combined ticket")
```

The source tickets stay archived evidence. The new ticket contains
`state/squash.json`, point-in-time source snapshots under
`artifacts/squashed-source-snapshots/`, links to each source, and the source
tickets' items and work-log entries copied into the target with source
provenance. Source artifact files stay in their original ticket directories;
the target records references back to those original paths. Each source also
gets a controlled archived annotation pointing back to the squash target.
That reverse pointer is stored only in sqlite `ticket_meta`, not in a sidecar
file.
An archived squash target can itself be used as a later squash source; after
that, the newer target becomes the continuing entry point and the older target
stays archived evidence with a reverse reference to the newer target.

## Design notes

- **sqlite is the truth source.** `TICKET.md` is a rendered view; never hand-edit
  it. Write commands update it automatically. A `render_revision` /
  `rendered_revision` pair keeps internal renders correct, and `fork` refreshes
  the source view internally before taking its point-in-time snapshot.
- **Schema v8, three core tables** (`ticket_meta`, `current_view`, legacy internal `notices`). Lifecycle
  lives in `ticket_meta.lifecycle_state`: `BACKLOG` means created but unclaimed,
  `ACTIVE` means claimed/in progress, and `ARCHIVED` means closed. sqlite-backed
  lease state also lives in `ticket_meta` and renders as the latest `Owner: claim/release by ...`
  operation in `TICKET.md`. `current_view` intentionally has a small mutable
  surface: required `goal`, optional `short_context`, bounded JSON list
  `must_remember`, URI `items`, a legacy artifact markdown field, work log, and supporting markdown
  fields. `Must remember` is for principles, preflight, invariants, and human
  instructions that should stay visible across compaction/handoff. Each ticket
  can hold at most 16 must-remember entries; delete one with `forget <index>`
  before adding another. `TICKET.md` renders these entries as a Markdown
  ordered list so the visible numbering matches `forget <index>`. Forked
  tickets copy these entries. `brief` puts the must-remember list, short
  context, and unread messages on the active read path; normal ticket-targeting
  commands also remind the agent when a ticket has must-remember entries.
  URI resources belong in `items` and render under `## Items`; the legacy
  artifact markdown field is retained for compatibility with older tickets and
  squash/fork annotations, not as the preferred resource-entry surface. The
  legacy internal `notices` table stores the external message inbox and
  checked/logged state.
- **`ticket_meta.ticket_dir` must match the current ticket directory.** sqlite is
  the source of truth; stale paths from moved/archive roots are rejected rather
  than silently repaired.
- **Owner ids are stored as local xurl session URIs.** Claim/new/fork/release
  infer the current agent from `CODEX_THREAD_ID` or `CLAUDE_CODE_SESSION_ID`
  and internally store `agents://<agent-type>/<session-id>` in sqlite. The
  referenced provider session must exist on this machine. If the command is not
  running inside a provider tool subprocess, pass `--agent-type codex|claude
  --session-id <uuid>` explicitly. There is no user/host/pane fallback.
- **Items are raw URIs.** Use `file://` for local filesystem paths and normal
  `https://` URLs for GitHub, docs, or any other web resource. The CLI
  stores and renders these strings unchanged in `TICKET.md`; agents can filter
  them with text tools such as `rg -n 'file://|https://|agents://' "$T/TICKET.md"`.
- **Squash is a post-hoc consolidation command, not a lifecycle.** It creates a
  normal target ticket from archived source tickets. The target can remain
  `ACTIVE` for continued work, be created `BACKLOG`, or be created already
  `ARCHIVED`. Source tickets are not physically merged or reopened; they keep
  their audit history and receive a controlled "Squashed into" reference.
- **Messages are an external inbox.** `message send` appends an unread active message to
  another ticket without changing its goal, context, item URI entries, legacy artifact field, lease, or
  workspace. Any successful command targeting a ticket with unread active messages
  emits a stderr reminder with the unread message summaries and tells the agent
  to read `TICKET.md`'s `## Messages` section. There is no `message list` command.
  `ticket <dir> message checked --until-id N` marks active messages through a waterline as checked and
  writes recognizable `[message #id]` entries into `Work log` idempotently.
  Message content should be concise summaries; embedded newlines are escaped so
  they render as one line. For longer context, write a markdown file under `notes/` or `artifacts/`
  and link it with `--with file://...`. Archived tickets reject normal active
  messages; `message send --allow-archived` can append historical context with
  no active holder/session delivery guarantee. See `references/message.md`.
- **Global search is derived state.** `AGENT_TICKETS_ROOT/search.sqlite3` is a
  root-level FTS5 index rebuilt from ticket truth sources; ticket writes try to
  upsert it automatically, and `aticket-cli tickets search --reindex` repairs drift if
  needed.
- **Current schema only.** The tool has not been published yet, so it only
  creates and reads the current schema shape.

## Tests

```bash
python3.12 -m pytest
```

In-process tests (no subprocess, no network).

The agent identity workflow is covered by the repo-local
`blank-agent-blackbox` skill:

```bash
skills/blank-agent-blackbox/SKILL.md
```

Run its manual blackbox test when changing the agent-facing lifecycle surface:

```bash
skills/blank-agent-blackbox/scripts/agent-env-blackbox.sh
```

The skill launches fresh Codex and Claude agents, injects `AGENT_TICKETS_ROOT`
from the parent process, prepends this checkout to `PYTHONPATH`, and asks each
agent to run `aticket-cli ticket new -> ticket <dir> release (BACKLOG) -> ticket <dir> claim (ACTIVE) -> ticket <dir> fork -> ticket <dir> release` without
`--agent-type` or `--session-id`. Its script then validates sqlite owner ids are
stored as `agents://codex/...` / `agents://claude/...` and that lifecycle
commands did not pass explicit owner flags.

Useful overrides:

```bash
ATICKET_BLACKBOX_CODEX_MODEL=gpt-5.4 \
ATICKET_BLACKBOX_CLAUDE_MODEL=sonnet \
ATICKET_BLACKBOX_OUT_DIR=/tmp/aticket-blackbox \
skills/blank-agent-blackbox/scripts/agent-env-blackbox.sh
```

Set `ATICKET_BLACKBOX_SKIP_CODEX=1` or `ATICKET_BLACKBOX_SKIP_CLAUDE=1` to run
only one provider. The test requires `aticket-cli`, `codex`, and/or `claude` on
`PATH`; `PYTHONPATH` is injected so an existing `aticket-cli` entrypoint imports this
checkout instead of a stale installed package.
