---
description: "Minimal external message inbox for held tickets."
triggers:
  - "aticket-cli message"
  - "message send"
  - "message checked"
  - "ticket inbox"
---

# Message - external ticket inbox

`message` is a minimal inbox for a held ticket. If ticket X is held by agent A,
agent B or another process can append an active message to X without taking over
the ticket lease. A receives the message by reading the rendered `TICKET.md`,
the same way agents already recover ticket state.

A future agent that claims the ticket also sees unread active messages in
`TICKET.md`, so supplemental information is not lost across handoff.

`message` is not a remote state-edit API. It only carries information.

Keep message content short and single-purpose. A message should be a one-line
summary. If the sender has substantial context, write that context to a
markdown note first, then send a one-line summary with `--with
file:///path/to/details.md`.

## Send A Message

```bash
aticket-cli message send \
  --ticket "$TARGET_TICKET_DIR" \
  "Please read the review summary before archiving." \
  --from-ticket "$SOURCE_TICKET_DIR" \
  --with file:///path/to/review-summary.md \
  --with https://github.com/example-org/example-repo/pull/123
```

- `--ticket` is the target ticket receiving the message.
- The positional message is required. Embedded newlines are escaped as literal
  `\n` so the message renders as one line, but long notes should still live in
  a file linked with `--with`.
- `--from-ticket` is optional and stores the source ticket as a `file://` URI.
- `--with` is optional and repeatable; values are stored on the message as
  related URIs. They do not create target-ticket item URI entries; use
  `aticket-cli ticket <dir> add-item` when the related URI should become a
  ticket resource entry. Each
  `--with` value must be a single-line URI.
- The sender is inferred as `agents://<provider>/<session-id>` by the same local
  identity rules used by `ticket new`, `ticket <dir> claim`, `ticket <dir> fork`,
  and `ticket <dir> release`.

On success, stdout prints the new message id. `TICKET.md` is re-rendered with an
unread `## Messages` section. Any later successful command targeting that ticket
emits a stderr reminder while the ticket still has unread active messages. The
reminder previews unread messages and tells the agent to read `TICKET.md`'s
`## Messages` section.

## Archived Tickets

Archived tickets reject normal active messages because no holder is guaranteed to
read and check them:

```bash
aticket-cli message send --ticket "$ARCHIVED_TICKET_DIR" "late active message"
```

If the sender only needs to append historical context, use `--allow-archived`:

```bash
aticket-cli message send \
  --ticket "$ARCHIVED_TICKET_DIR" \
  --allow-archived \
  "Historical context; no active delivery expected."
```

This appends a message under `## Messages` with an archived/no-delivery marker.
It is searchable, but it does not count as unread, does not block archive, and no
active holder/session is guaranteed to receive it.

## Receive A Message

There is no `message list` command. Read the ticket file:

```bash
sed -n '1,180p' "$TICKET_DIR/TICKET.md"
```

Unread active messages render as:

```text
## Messages

Unread: 1

- [message #17] 2026-06-07T00:20:00+08:00 Please read the review summary before archiving. from=file:///code/tsshi/agent-tickets/tickets/2026-06-06-source with=file:///tmp/review-summary.md with=https://github.com/example-org/example-repo/pull/123 by=agents://codex/019e...
```

The summary comes before metadata so `rg -n '\[message #'` shows the important
message immediately. Metadata is appended as `from=...`, repeated `with=...`,
and `by=...`. The sender is audit metadata; the actionable context is usually
the source ticket and related URIs.

Unread reminders do not mark messages checked. The holder must read/handle the
message and then explicitly advance the checked waterline.

## Mark Messages Checked

```bash
aticket-cli ticket "$TICKET_DIR" message checked --until-id 18
```

This marks all unchecked active messages with `id <= 18` as checked, removes
them from the unread part of `## Messages`, and appends recognizable message
entries to `## Work log`:

```text
- 00:25:10: [message #17] Please read the review summary before archiving. from=file:///code/tsshi/agent-tickets/tickets/...
- 00:25:11: Checked messages until #18.
```

The operation is idempotent. Re-running the same `--until-id` does not duplicate
message log entries. Archived historical messages are not checkable.

## Boundaries

`message` does not:

- change `goal`
- change `short-context`
- change item URI entries or the legacy artifact field
- affect `claim` or `release`
- touch `workspace`
- grant the sender permission to advance the target ticket

Archive has one special interaction: active unread messages block archive until
checked; archived historical messages created with `--allow-archived` do not.

The holder decides whether a message should become an action, a normal log
entry, an item, or a follow-up ticket.
