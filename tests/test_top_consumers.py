"""top_consumers: costliest sessions or command labels in a window."""
import json
import subprocess
import sys
from pathlib import Path

from conftest import assistant, usage, user, write_jsonl

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "token_usage.py"


def seed(tmp_path, monkeypatch):
    proj = tmp_path / "projects"
    # s1: /review 100 out on fable-5 ($5.00); (no command) turn first.
    write_jsonl(proj / "-Users-x-one" / "s1.jsonl", [
        user("2026-06-10T10:00:00Z"),
        assistant("2026-06-10T10:00:01Z", usage(out=10), request_id="r0"),
        user("2026-06-10T10:01:00Z", command="/review"),
        assistant("2026-06-10T10:01:01Z", usage(out=100_000), request_id="r1"),
    ])
    # s2: /review 40k + /commit 20k on fable-5.
    write_jsonl(proj / "-Users-x-two" / "s2.jsonl", [
        user("2026-06-12T10:00:00Z", command="/review"),
        assistant("2026-06-12T10:00:01Z", usage(out=40_000), request_id="r2"),
        user("2026-06-12T10:02:00Z", command="/commit"),
        assistant("2026-06-12T10:02:01Z", usage(out=20_000), request_id="r3"),
    ])
    # s3: unpriced model — must sort last, never crash.
    write_jsonl(proj / "-Users-x-two" / "s3.jsonl", [
        user("2026-06-13T10:00:00Z", command="/mystery"),
        assistant("2026-06-13T10:00:01Z", usage(out=999_999),
                  model="claude-mystery-9", request_id="r4"),
    ])
    monkeypatch.setenv("TOKEN_USAGE_PROJECTS_DIR", str(proj))
    monkeypatch.setenv("TOKEN_USAGE_LEDGER_DIR", str(tmp_path / "cache"))
    return proj


def test_by_session_sorted_by_cost_unpriced_last(tu, tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    data = tu.run_top_consumers(by="session", since="2026-01-01")
    ids = [r["session_id"] for r in data["rows"]]
    assert ids == ["s1", "s2", "s3"]
    assert data["rows"][0]["project"] == "-Users-x-one"
    assert data["rows"][0]["first_ts"] == "2026-06-10T10:00:00Z"
    assert data["rows"][0]["cost_usd"] > data["rows"][1]["cost_usd"] > 0
    assert data["rows"][2]["cost_usd"] is None
    assert data["unpriced_models"] == ["claude-mystery-9"]


def test_by_command_aggregates_across_sessions(tu, tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    data = tu.run_top_consumers(by="command", since="2026-01-01")
    rows = {r["label"]: r for r in data["rows"]}
    assert rows["/review"]["sessions"] == 2
    assert rows["/review"]["invocations"] == 2
    assert rows["/review"]["usage"]["output"] == 140_000
    assert [r["label"] for r in data["rows"]][:2] == ["/review", "/commit"]
    assert data["rows"][-1]["label"] == "/mystery"          # unpriced last


def test_limit_and_filters(tu, tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    assert len(tu.run_top_consumers(by="session", since="2026-01-01", limit=1)["rows"]) == 1
    only_two = tu.run_top_consumers(by="session", since="2026-01-01", project="x-two")
    assert {r["project"] for r in only_two["rows"]} == {"-Users-x-two"}
    later = tu.run_top_consumers(by="session", since="2026-06-11")
    assert [r["session_id"] for r in later["rows"]] == ["s2", "s3"]


def test_render_by_session_and_by_command(tu, tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    out = tu.render_top_consumers(tu.run_top_consumers(by="session", since="2026-01-01"))
    assert out.startswith("| Session | Project | Started |")
    assert "| s1 | -Users-x-one | 2026-06-10 |" in out
    assert "unpriced" in out and "claude-mystery-9" in out
    out = tu.render_top_consumers(tu.run_top_consumers(by="command", since="2026-01-01"))
    assert out.startswith("| Command | Sessions | Calls |")
    assert "| `/review` | 2 | 2 |" in out


def test_render_empty_window(tu, tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    out = tu.render_top_consumers(tu.run_top_consumers(by="session", since="2030-01-01"))
    assert out == "No sessions in window."


def test_cli_top_consumers_json(tu, tmp_path, monkeypatch):
    proj = seed(tmp_path, monkeypatch)
    import os
    env = dict(os.environ, TOKEN_USAGE_PROJECTS_DIR=str(proj),
               TOKEN_USAGE_LEDGER_DIR=str(tmp_path / "cache"))
    r = subprocess.run([sys.executable, str(SCRIPT), "top_consumers", "--by", "command",
                        "--since", "2026-01-01", "--limit", "2", "--json"],
                       capture_output=True, text=True, env=env, check=True)
    data = json.loads(r.stdout)
    assert data["by"] == "command" and data["limit"] == 2
    assert [row["label"] for row in data["rows"]] == ["/review", "/commit"]


def test_session_mode_discloses_unpriced_rows_cut_by_the_limit(tu, tmp_path, monkeypatch):
    # Unpriced sessions sort last, so a --limit is exactly what hides them —
    # the count must survive the truncation that dropped them.
    seed(tmp_path, monkeypatch)
    full = tu.run_top_consumers(by="session", since="2026-01-01")
    assert full["unpriced_rows"] == 1
    assert "cut by --limit" not in tu.render_top_consumers(full)
    cut = tu.run_top_consumers(by="session", since="2026-01-01", limit=2)
    assert cut["unpriced_rows"] == 1
    assert [r["session_id"] for r in cut["rows"]] == ["s1", "s2"]
    assert "1 unpriced session(s) rank last and were cut by --limit" in \
        tu.render_top_consumers(cut)


def test_command_mode_flags_partially_priced_labels(tu, tmp_path, monkeypatch):
    # /review runs in two sessions, one of them on an unpriced model: the
    # cost shown is the priced subtotal only, which has to be disclosed.
    proj = seed(tmp_path, monkeypatch)
    write_jsonl(proj / "-Users-x-two" / "s4.jsonl", [
        user("2026-06-14T10:00:00Z", command="/review"),
        assistant("2026-06-14T10:00:01Z", usage(out=500_000),
                  model="claude-mystery-9", request_id="r5"),
    ])
    data = tu.run_top_consumers(by="command", since="2026-01-01")
    rows = {r["label"]: r for r in data["rows"]}
    assert rows["/review"]["partial"] is True
    assert rows["/review"]["cost_usd"] == 7.0        # 140k out @ $50/MTok, priced only
    assert rows["/commit"]["partial"] is False
    out = tu.render_top_consumers(data)
    assert "partially priced (some sessions on unpriced models)" in out


def test_render_empty_window_names_the_grouping(tu, tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    empty_cmds = tu.run_top_consumers(by="command", since="2030-01-01")
    assert tu.render_top_consumers(empty_cmds) == "No commands in window."


def test_render_command_mode_leaves_the_no_command_row_unquoted(tu, tmp_path, monkeypatch):
    # "(no command)" is a bucket name, not a command anyone can type, so it
    # must not be dressed up as code.
    seed(tmp_path, monkeypatch)
    out = tu.render_top_consumers(tu.run_top_consumers(by="command", since="2026-01-01"))
    assert f"| {tu.OTHER_LABEL} |" in out
    assert f"`{tu.OTHER_LABEL}`" not in out


def test_cli_top_consumers_markdown_and_bad_limit(tu, tmp_path, monkeypatch):
    import os
    proj = seed(tmp_path, monkeypatch)
    env = dict(os.environ, TOKEN_USAGE_PROJECTS_DIR=str(proj),
               TOKEN_USAGE_LEDGER_DIR=str(tmp_path / "cache"))
    r = subprocess.run([sys.executable, str(SCRIPT), "top_consumers",
                        "--since", "2026-01-01"],
                       capture_output=True, text=True, env=env, check=True)
    assert r.stdout.startswith("| Session | Project | Started |")
    assert "| s1 | -Users-x-one | 2026-06-10 |" in r.stdout
    r = subprocess.run([sys.executable, str(SCRIPT), "top_consumers", "--limit", "0"],
                       capture_output=True, text=True, env=env, check=False)
    assert r.returncode != 0
    assert r.stderr.strip() == "token-usage: --limit must be >= 1"
