"""Render round-trip: rendered TICKET.md parses back to the same field values."""
from pathlib import Path

from conftest import make_ticket, run, seed_legacy_artifacts
from ticket_cli.mdparse import parse_ticket_md


def test_render_roundtrip_sections(tickets_root):
    t = make_ticket()
    run(["ticket", str(t), "goal", "round trip goal"])
    run(["ticket", str(t), "context", "step one; then step two"])
    run(["ticket", str(t), "remember", "Preflight: load AGENTS.md"])
    run(["ticket", str(t), "log", "did a thing"])
    seed_legacy_artifacts(t, "report.md")

    title, sections = parse_ticket_md(Path(t) / "TICKET.md")
    assert title.startswith("# Ticket:")
    assert sections["Goal"].strip() == "round trip goal"
    assert "step one" in sections["Short context"]
    assert "Preflight: load AGENTS.md" in sections["Must remember"]
    assert "did a thing" in sections["Work log"]
    assert "report.md" in sections["Artifacts"]

    md = (Path(t) / "TICKET.md").read_text(encoding="utf-8")
    assert md.index("## Short context") < md.index("## Must remember") < md.index("## Work log")


def test_items_render_under_items_section(tickets_root):
    t = make_ticket()
    run(["ticket", str(t), "add-item", "https://example.test/pr/1"])

    md = (Path(t) / "TICKET.md").read_text(encoding="utf-8")
    assert "## Items\n- https://example.test/pr/1" in md
    assert "## Links" not in md


def test_managed_marker_on_first_line(tickets_root):
    t = make_ticket()
    assert (Path(t) / "TICKET.md").read_text().splitlines()[0] == "<!-- managed-by: sqlite -->"
