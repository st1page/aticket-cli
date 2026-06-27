---
name: blank-agent-blackbox
description: "Run aticket's fresh Codex/Claude blackbox usability test. Use when validating whether a blank agent can use the ticket CLI lifecycle from public help/docs, with AGENT_TICKETS_ROOT injected by the parent process and no explicit --agent-type/--session-id owner flags."
---

# Blank Agent Blackbox

Use this skill to validate aticket as an agent-facing tool, not as a unit-test-only library. It launches fresh Codex and Claude child agents and checks whether they can complete the ticket lifecycle from the public CLI surface.

## What It Tests

- Parent process injects a temporary `AGENT_TICKETS_ROOT`.
- Parent process prepends the current checkout to `PYTHONPATH`, so a global `aticket-cli` entrypoint imports this repo instead of a stale installed package.
- Child agents run `aticket-cli ticket new -> ticket <dir> release -> ticket <dir> claim -> ticket <dir> fork -> ticket <dir> release`.
- Child agents must not pass `--agent-type` or `--session-id`.
- The script validates sqlite owner ids and scans the child-agent command trace.

This is intentionally a manual blackbox test. It starts real Codex/Claude sessions and should not run as part of normal `pytest`.

## Run

From the aticket repo/worktree:

```bash
skills/blank-agent-blackbox/scripts/agent-env-blackbox.sh
```

Useful overrides:

```bash
ATICKET_BLACKBOX_CODEX_MODEL=gpt-5.4 \
ATICKET_BLACKBOX_CLAUDE_MODEL=sonnet \
ATICKET_BLACKBOX_OUT_DIR=/tmp/aticket-blackbox \
skills/blank-agent-blackbox/scripts/agent-env-blackbox.sh
```

Run only one provider:

```bash
ATICKET_BLACKBOX_SKIP_CODEX=1 skills/blank-agent-blackbox/scripts/agent-env-blackbox.sh
ATICKET_BLACKBOX_SKIP_CLAUDE=1 skills/blank-agent-blackbox/scripts/agent-env-blackbox.sh
```

## Read Results

The script prints the output directory and writes:

- `SUMMARY.md`
- `codex-events.jsonl`
- `codex-last-message.txt`
- `codex-stderr.log`
- `claude-output.json`
- `claude-stderr.log`
- per-provider temporary ticket roots

Do not trust only the child agent's final message. Check that sqlite owner ids use `agents://codex/...` / `agents://claude/...` and that lifecycle commands did not include explicit owner fallback flags.

## Common Failures

- `aticket-cli entrypoint not found`: install this checkout with `pip install -e .`, or ensure `aticket-cli` is on `PATH`.
- Stale global `aticket-cli`: keep the script's `PYTHONPATH` injection; it is there so the global console script imports this checkout.
- Missing provider transcript: the current Codex/Claude installation did not persist the child session where the script expects it.
- Provider format drift: if Codex/Claude change their JSONL transcript shape, update only this skill's script and keep the CLI behavior unchanged.
