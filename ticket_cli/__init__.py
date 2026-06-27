"""ticket_cli — a consolidated CLI for agent work tickets.

A ticket is the repo work unit: a directory holding TICKET.md (a rendered view),
machine-readable state (sqlite, including lease state) under state/, plus notes/,
artifacts/, and workspace/. This package replaces the previously scattered
"session" shell/python scripts with the single `aticket-cli` entrypoint.
"""

__version__ = "0.1.0"
