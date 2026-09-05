# token-usage 0.6.0 MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a stdlib stdio MCP server that exposes token-usage cost queries (`session_cost`, `history`, `insights`, `diff`, `top_consumers`) to Claude Code (auto-registered by the plugin) and Claude desktop.

**Architecture:** A new `scripts/mcp_server.py` speaks newline-delimited JSON-RPC 2.0 over stdio and delegates every query to functions in `scripts/token_usage.py`. The analyser gains two things the server needs: a non-exiting transcript locator that understands session ids and an explicit project dir, and a `top_consumers` query (also exposed as a CLI subcommand). Tool failures are MCP results with `isError: true`; only protocol-level problems become JSON-RPC errors.

**Tech Stack:** Python 3.9+ stdlib only (json, sys, os, re, pathlib, argparse). pytest for tests. No MCP SDK.

**Spec:** `docs/superpowers/specs/2026-09-04-token-usage-0.6.0-mcp-server-design.md`

## Global Constraints

- Python 3.9+ stdlib only. Zero dependencies. No install step. (No `match`, no `X | Y` type annotations, no `str.removeprefix` reliance beyond 3.9.)
- No LLM calls, no network, no telemetry.
- Nothing the server does can affect the hook path or the ledger.
- `scripts/token_usage.py` stays the single analyser; `scripts/mcp_server.py` imports it and contains no cost logic.
- stdout of the server carries JSON-RPC only; logging goes to stderr.
- Tool results are JSON by default (`format: "json"`), rendered text with `format: "markdown"`.
- Commit messages end with:
  ```
  Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01PqaqeJS8JsAU1To3fvsPH5
  ```
- Run tests with the scratchpad venv: `/private/tmp/claude-501/-Users-craigfletcher-Developer-GitHub-WizzoUK2-token-usage/78b38d68-11f7-4d4a-89a8-e58e6844d5d6/scratchpad/venv/bin/python -m pytest` (referred to below as `$PY -m pytest`). If that venv is gone: `python3.13 -m venv <scratchpad>/venv && <venv>/bin/pip install pytest`.

## File structure

| File | Responsibility |
|---|---|
| `scripts/token_usage.py` (modify) | Analyser. Gains `locate_transcript()`, `find_latest_transcript(project_dir=None)` using `projects_dir()`, `run_top_consumers()`, `render_top_consumers()`, and the `top_consumers` CLI subcommand. |
| `scripts/mcp_server.py` (create) | MCP protocol loop (`serve`), request router (`handle_message`), tool schemas (`TOOLS`), argument validation (`validate_args`), dispatch with error wrapping (`call_tool`), five tool handlers. |
| `.mcp.json` (create) | Plugin MCP registration for Claude Code. |
| `tests/conftest.py` (modify) | Adds an `mcp` fixture that loads `mcp_server.py` bound to the same `token_usage` module instance the `tu` fixture uses. |
| `tests/test_locate.py` (create) | Transcript resolution order. |
| `tests/test_top_consumers.py` (create) | `run_top_consumers` / `render_top_consumers` / CLI. |
| `tests/test_mcp.py` (create) | Protocol, schemas, validation, each tool, error paths, stdout guard, subprocess smoke test. |
| `README.md`, `skills/report/SKILL.md`, `CHANGELOG.md`, `.claude-plugin/plugin.json` (modify) | Docs and 0.6.0 release. |

---

### Task 1: Non-exiting transcript locator with session id and project dir

**Files:**
- Modify: `scripts/token_usage.py:1057-1089` (`find_latest_transcript`, `resolve_transcript`)
- Modify: `docs/superpowers/specs/2026-09-04-token-usage-0.6.0-mcp-server-design.md` (resolution order note)
- Test: `tests/test_locate.py`

**Interfaces:**
- Consumes: `projects_dir()`, `project_slug(path_str)` (existing).
- Produces:
  - `find_latest_transcript(project_dir=None) -> Path | None` — newest `.jsonl` under `<projects_dir>/<slug(project_dir or cwd)>/`, then the Cowork mount roots, then the newest `.jsonl` under any project in `projects_dir()`.
  - `locate_transcript(arg=None, session_id=None, project_dir=None) -> Path | None` — explicit path (must be a file), else session id glob `<projects_dir>/*/<id>.jsonl` (newest by mtime on several matches), else `TOKEN_USAGE_TRANSCRIPT` env, else `find_latest_transcript(project_dir)`.
  - `resolve_transcript(arg)` keeps its signature; now a wrapper that exits with the existing message when `locate_transcript` returns `None`.

Note on order: the spec lists "newest anywhere" before the Cowork mounts. The Cowork mount, when present, contains exactly the live session, so it is the more precise signal; this task checks it first and updates the spec sentence to match.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_locate.py`:

```python
"""Transcript resolution order used by the CLI and the MCP server."""
import os

from conftest import assistant, usage, user, write_jsonl


def seed(tmp_path, monkeypatch):
    proj = tmp_path / "projects"
    a = write_jsonl(proj / "-Users-x-alpha" / "aaa-111.jsonl", [
        user("2026-06-10T10:00:00Z"),
        assistant("2026-06-10T10:00:01Z", usage(out=10), request_id="r1"),
    ])
    b = write_jsonl(proj / "-Users-x-beta" / "bbb-222.jsonl", [
        user("2026-06-12T10:00:00Z"),
        assistant("2026-06-12T10:00:01Z", usage(out=20), request_id="r2"),
    ])
    # Make b the newest file on disk regardless of write order.
    os.utime(a, (1_700_000_000, 1_700_000_000))
    os.utime(b, (1_700_000_100, 1_700_000_100))
    monkeypatch.setenv("TOKEN_USAGE_PROJECTS_DIR", str(proj))
    monkeypatch.delenv("TOKEN_USAGE_TRANSCRIPT", raising=False)
    return proj, a, b


def test_explicit_path_wins_and_must_exist(tu, tmp_path, monkeypatch):
    proj, a, b = seed(tmp_path, monkeypatch)
    assert tu.locate_transcript(str(a)) == a
    assert tu.locate_transcript(str(tmp_path / "missing.jsonl")) is None


def test_session_id_is_searched_across_projects(tu, tmp_path, monkeypatch):
    proj, a, b = seed(tmp_path, monkeypatch)
    assert tu.locate_transcript(session_id="aaa-111") == a
    assert tu.locate_transcript(session_id="nope-999") is None
    # Path characters are stripped so an id can never escape the projects dir.
    assert tu.locate_transcript(session_id="../../etc/passwd") is None


def test_project_dir_picks_that_projects_newest(tu, tmp_path, monkeypatch):
    proj, a, b = seed(tmp_path, monkeypatch)
    # slug("/Users/x/alpha") == "-Users-x-alpha"
    assert tu.locate_transcript(project_dir="/Users/x/alpha") == a
    assert tu.find_latest_transcript(project_dir="/Users/x/beta") == b


def test_unknown_project_falls_back_to_newest_anywhere(tu, tmp_path, monkeypatch):
    proj, a, b = seed(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)  # cwd slug matches no project either
    assert tu.locate_transcript(project_dir="/Users/x/nothing-here") == b
    assert tu.locate_transcript() == b


def test_env_transcript_overrides_discovery(tu, tmp_path, monkeypatch):
    proj, a, b = seed(tmp_path, monkeypatch)
    monkeypatch.setenv("TOKEN_USAGE_TRANSCRIPT", str(a))
    assert tu.locate_transcript(project_dir="/Users/x/beta") == a


def test_resolve_transcript_exits_when_nothing_found(tu, tmp_path, monkeypatch):
    import pytest
    monkeypatch.setenv("TOKEN_USAGE_PROJECTS_DIR", str(tmp_path / "empty"))
    monkeypatch.delenv("TOKEN_USAGE_TRANSCRIPT", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as e:
        tu.resolve_transcript(None)
    assert "no transcript found" in str(e.value)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$PY -m pytest tests/test_locate.py -q`
Expected: failures with `AttributeError: module 'token_usage' has no attribute 'locate_transcript'` and a `TypeError` for the `project_dir` keyword.

- [ ] **Step 3: Implement the locator**

Replace `find_latest_transcript` and `resolve_transcript` in `scripts/token_usage.py` with:

```python
def _newest(paths):
    paths = list(paths)
    return max(paths, key=lambda p: p.stat().st_mtime) if paths else None


def find_latest_transcript(project_dir=None):
    """Newest transcript for a project (default: cwd), or None.

    Order: the project's own directory under projects_dir(); the Cowork sandbox
    mounts (which hold exactly the live session); then the newest transcript in
    any project — the Claude desktop case, where there is no project dir."""
    root = projects_dir()
    # 1) Claude Code: <projects>/<slug>/*.jsonl
    slug_dir = root / project_slug(str(project_dir or Path.cwd()))
    if slug_dir.is_dir():
        f = _newest(slug_dir.glob("*.jsonl"))
        if f:
            return f
    # 2) Cowork (Claude desktop app): the sandbox mounts the live session's
    #    transcript read-only under <mount>/.claude/projects/<slug>/<session>.jsonl.
    cowork_roots = [Path.home() / "mnt" / ".claude" / "projects"]
    sessions = Path("/sessions")
    if sessions.is_dir():
        cowork_roots.extend(sorted(sessions.glob("*/mnt/.claude/projects")))
    for r in cowork_roots:
        if r.is_dir():
            f = _newest(r.glob("*/*.jsonl"))
            if f:
                return f
    # 3) No project context at all: newest transcript on the machine.
    return _newest(root.glob("*/*.jsonl")) if root.is_dir() else None


def locate_transcript(arg=None, session_id=None, project_dir=None):
    """Transcript to analyse, or None. Explicit path (must exist) > session id
    (searched across every project) > TOKEN_USAGE_TRANSCRIPT > newest for the
    project dir (see find_latest_transcript)."""
    if arg:
        p = Path(arg)
        return p if p.is_file() else None
    if session_id:
        safe = re.sub(r"[^A-Za-z0-9_-]", "", str(session_id))
        return _newest(projects_dir().glob(f"*/{safe}.jsonl")) if safe else None
    env = os.environ.get("TOKEN_USAGE_TRANSCRIPT")
    if env:
        return Path(env)
    return find_latest_transcript(project_dir)


def resolve_transcript(arg):
    t = locate_transcript(arg)
    if t:
        return t
    sys.exit("token-usage: no transcript found — pass a path to a session .jsonl file")
```

- [ ] **Step 4: Run the whole suite**

Run: `$PY -m pytest tests/ -q`
Expected: all pass (98 existing + 6 new). If `test_hook.py` or `test_parsing.py` break, the cause is `resolve_transcript` now rejecting a non-existent explicit path — check the failing test's fixture writes the file it names.

- [ ] **Step 5: Amend the spec's resolution order**

In the spec's "Session resolution" list, change item 4 to:

```
4. the Cowork mount roots (they hold exactly the live session), then the
   newest `.jsonl` under any project (Claude desktop, where no project dir
   exists).
```

- [ ] **Step 6: Commit**

```bash
git add scripts/token_usage.py tests/test_locate.py docs/superpowers/specs/2026-09-04-token-usage-0.6.0-mcp-server-design.md
git commit -m "feat: locate_transcript — session-id lookup, explicit project dir, non-exiting" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01PqaqeJS8JsAU1To3fvsPH5"
```

---

### Task 2: `top_consumers` query, renderer and CLI subcommand

**Files:**
- Modify: `scripts/token_usage.py` — add `run_top_consumers` and `render_top_consumers` after `render_history` (around line 826); add the subcommand in `main()`.
- Test: `tests/test_top_consumers.py`

**Interfaces:**
- Consumes: `load_pricing()`, `since_cutoff(arg)`, `projects_dir()`, `cached_summary(path, pricing)`, `unpriced_models(by_model, pricing)`, `unpriced_footnote(models)`, `empty_usage()`, `fmt_tokens`, `fmt_cost`, `OTHER_LABEL`.
- Produces:
  - `run_top_consumers(by="session", since="30d", project=None, limit=10) -> dict` with keys `by, since, project, limit, rows, unpriced_models`.
    - `by == "session"` rows: `{"session_id", "path", "project", "first_ts", "usage", "cost_usd"}`.
    - `by == "command"` rows: `{"label", "sessions", "invocations", "usage", "cost_usd"}`.
    - Sorted by cost desc; unpriced (`cost_usd is None`) rows last; ties by id/label asc; truncated to `limit`.
  - `render_top_consumers(data) -> str` markdown table.
  - CLI: `top_consumers [--by session|command] [--since 30d] [--project SUB] [--limit 10] [--json]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_top_consumers.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$PY -m pytest tests/test_top_consumers.py -q`
Expected: `AttributeError: ... has no attribute 'run_top_consumers'`; the CLI test fails with `CalledProcessError` (argparse "invalid choice").

- [ ] **Step 3: Implement the query and renderer**

Insert after `render_history` (before the `# --- Insights ---` comment block) in `scripts/token_usage.py`:

```python
def run_top_consumers(by="session", since="30d", project=None, limit=10):
    """Costliest sessions (by="session") or command labels aggregated across
    sessions (by="command") in a window. Unpriced rows sort last."""
    pricing = load_pricing()
    cutoff = since_cutoff(since)
    unpriced = set()
    sessions, commands = [], {}
    for f in sorted(projects_dir().glob("*/*.jsonl")):
        try:
            s, _ = cached_summary(f, pricing)
        except OSError:
            continue
        if cutoff and (s["first_ts"] or "") < cutoff:
            continue
        if project and project not in s["project"]:
            continue
        unpriced.update(unpriced_models(s.get("by_model", {}), pricing))
        if by == "session":
            sessions.append({"session_id": Path(s["path"]).stem, "path": s["path"],
                             "project": s["project"], "first_ts": s["first_ts"],
                             "usage": s["total"]["usage"],
                             "cost_usd": s["total"]["cost_usd"]})
            continue
        for label, agg in s["by_label"].items():
            c = commands.setdefault(label, {"label": label, "sessions": 0, "invocations": 0,
                                            "usage": empty_usage(), "cost_usd": None})
            c["sessions"] += 1
            c["invocations"] += agg["invocations"]
            for k in c["usage"]:
                c["usage"][k] += agg["usage"].get(k, 0)
            if agg["cost_usd"] is not None:
                c["cost_usd"] = (c["cost_usd"] or 0.0) + agg["cost_usd"]
    rows = sessions if by == "session" else list(commands.values())
    key = "session_id" if by == "session" else "label"
    rows.sort(key=lambda r: (r["cost_usd"] is None, -(r["cost_usd"] or 0.0), r[key]))
    return {"by": by, "since": since, "project": project, "limit": limit,
            "rows": rows[:limit], "unpriced_models": sorted(unpriced)}


def render_top_consumers(data):
    if not data["rows"]:
        return "No sessions in window."
    if data["by"] == "session":
        lines = ["| Session | Project | Started | Output | Input | Cache read | Cache write | Est. cost |",
                 "|---|---|---|---:|---:|---:|---:|---:|"]
        for r in data["rows"]:
            u = r["usage"]
            lines.append(f"| {r['session_id']} | {r['project']} | {(r['first_ts'] or '')[:10]} "
                         f"| {fmt_tokens(u['output'])} | {fmt_tokens(u['input'])} "
                         f"| {fmt_tokens(u['cache_read'])} | {fmt_tokens(u['cache_5m'] + u['cache_1h'])} "
                         f"| {fmt_cost(r['cost_usd'])} |")
    else:
        lines = ["| Command | Sessions | Calls | Output | Input | Cache read | Cache write | Est. cost |",
                 "|---|---:|---:|---:|---:|---:|---:|---:|"]
        for r in data["rows"]:
            u = r["usage"]
            name = r["label"] if r["label"] == OTHER_LABEL else f"`{r['label']}`"
            lines.append(f"| {name} | {r['sessions']} | {r['invocations']} "
                         f"| {fmt_tokens(u['output'])} | {fmt_tokens(u['input'])} "
                         f"| {fmt_tokens(u['cache_read'])} | {fmt_tokens(u['cache_5m'] + u['cache_1h'])} "
                         f"| {fmt_cost(r['cost_usd'])} |")
    note = unpriced_footnote(data.get("unpriced_models") or [])
    if note:
        lines += ["", note]
    return "\n".join(lines)
```

- [ ] **Step 4: Add the CLI subcommand**

In `main()`, after the `insights` parser definition (`i.add_argument("--json", ...)`), add:

```python
    t = sub.add_parser("top_consumers")
    t.add_argument("--by", choices=("session", "command"), default="session")
    t.add_argument("--since", default="30d")
    t.add_argument("--project", default=None)
    t.add_argument("--limit", type=int, default=10)
    t.add_argument("--json", action="store_true", dest="as_json")
```

and after the `if args.cmd == "insights":` block add:

```python
    if args.cmd == "top_consumers":
        if args.limit < 1:
            sys.exit("token-usage: --limit must be >= 1")
        data = run_top_consumers(by=args.by, since=args.since,
                                 project=args.project, limit=args.limit)
        print(json.dumps(data, indent=1) if args.as_json else render_top_consumers(data))
        return
```

Also update the module docstring's `Subcommands:` list (top of file) with:

```
    top_consumers         Costliest sessions or commands in a window (--by, --since, --limit)
```

- [ ] **Step 5: Run the suite**

Run: `$PY -m pytest tests/ -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/token_usage.py tests/test_top_consumers.py
git commit -m "feat: top_consumers — costliest sessions or commands in a window (query, renderer, CLI)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01PqaqeJS8JsAU1To3fvsPH5"
```

---

### Task 3: Server skeleton — protocol loop, initialize, ping, errors

**Files:**
- Create: `scripts/mcp_server.py`
- Modify: `tests/conftest.py` (add `mcp` fixture)
- Test: `tests/test_mcp.py`

**Interfaces:**
- Consumes: `token_usage` module (imported as `tu` inside the server).
- Produces:
  - `handle_message(msg: dict) -> dict | None` — a JSON-RPC response object, or `None` for notifications.
  - `serve(stdin=None, stdout=None) -> None` — reads newline-delimited JSON until EOF, writes replies.
  - `SUPPORTED_VERSIONS`, `LATEST_VERSION`, `plugin_version()`.
  - `_result(id_, result)`, `_error(id_, code, message)` helpers.
  - `TOOLS = []` placeholder list and a `tools/list` branch that returns it (filled in Task 4).

- [ ] **Step 1: Add the `mcp` fixture to conftest**

Append to `tests/conftest.py`:

```python
SERVER = Path(__file__).resolve().parent.parent / "scripts" / "mcp_server.py"


@pytest.fixture
def mcp():
    """The MCP server module, bound to the SAME token_usage instance as `tu`
    so monkeypatching either side is visible to both."""
    import sys
    sys.modules.setdefault("token_usage", _tu)
    spec = importlib.util.spec_from_file_location("mcp_server", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_mcp.py`:

```python
"""MCP server: JSON-RPC framing, handshake, tools, error paths."""
import io
import json


def req(method, id_=1, **params):
    m = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params:
        m["params"] = params
    return m


def notif(method, **params):
    m = {"jsonrpc": "2.0", "method": method}
    if params:
        m["params"] = params
    return m


def test_initialize_echoes_supported_version(mcp):
    r = mcp.handle_message(req("initialize", protocolVersion="2024-11-05",
                               capabilities={}, clientInfo={"name": "t", "version": "0"}))
    assert r["id"] == 1 and "error" not in r
    assert r["result"]["protocolVersion"] == "2024-11-05"
    assert r["result"]["capabilities"] == {"tools": {}}
    assert r["result"]["serverInfo"]["name"] == "token-usage"


def test_initialize_falls_back_to_latest_for_unknown_version(mcp):
    r = mcp.handle_message(req("initialize", protocolVersion="2099-01-01"))
    assert r["result"]["protocolVersion"] == mcp.LATEST_VERSION


def test_server_version_matches_plugin_manifest(mcp):
    from pathlib import Path
    manifest = json.loads((Path(mcp.__file__).resolve().parent.parent
                           / ".claude-plugin" / "plugin.json").read_text())
    r = mcp.handle_message(req("initialize", protocolVersion="2025-06-18"))
    assert r["result"]["serverInfo"]["version"] == manifest["version"]


def test_notifications_get_no_response(mcp):
    assert mcp.handle_message(notif("notifications/initialized")) is None
    assert mcp.handle_message(notif("notifications/whatever")) is None


def test_ping(mcp):
    assert mcp.handle_message(req("ping", id_=7)) == {"jsonrpc": "2.0", "id": 7, "result": {}}


def test_unknown_method_is_method_not_found(mcp):
    r = mcp.handle_message(req("resources/list", id_=3))
    assert r["id"] == 3 and r["error"]["code"] == -32601


def test_invalid_request_shape(mcp):
    r = mcp.handle_message({"jsonrpc": "2.0", "id": 4})          # no method
    assert r["error"]["code"] == -32600 and r["id"] == 4
    r = mcp.handle_message([1, 2, 3])                            # batch/array
    assert r["error"]["code"] == -32600 and r["id"] is None


def test_serve_frames_one_json_per_line_and_survives_garbage(mcp):
    stdin = io.StringIO("\n".join([
        json.dumps(req("initialize", id_=1, protocolVersion="2025-06-18")),
        "this is not json",
        json.dumps(notif("notifications/initialized")),
        json.dumps(req("ping", id_=2)),
        "",
    ]) + "\n")
    stdout = io.StringIO()
    mcp.serve(stdin=stdin, stdout=stdout)
    lines = [json.loads(l) for l in stdout.getvalue().splitlines()]
    assert [l.get("id") for l in lines] == [1, None, 2]
    assert lines[1]["error"]["code"] == -32700
    assert lines[2]["result"] == {}
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `$PY -m pytest tests/test_mcp.py -q`
Expected: every test errors at fixture setup with `FileNotFoundError` for `scripts/mcp_server.py`.

- [ ] **Step 4: Create the server skeleton**

Create `scripts/mcp_server.py`:

```python
#!/usr/bin/env python3
"""token-usage MCP server — stdio JSON-RPC 2.0 front-end for token_usage.py.

Exposes cost queries as MCP tools (session_cost, history, insights, diff,
top_consumers). Claude Code starts it from the plugin's .mcp.json; Claude
desktop can register it as a local stdio server. Stdlib only, Python 3.9+.

Framing: one JSON-RPC object per line on stdin/stdout. stdout carries
JSON-RPC only; diagnostics go to stderr.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import token_usage as tu  # noqa: E402

SUPPORTED_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
LATEST_VERSION = "2025-06-18"

TOOLS = []  # filled in below (tool definitions)


def plugin_version():
    manifest = Path(__file__).resolve().parent.parent / ".claude-plugin" / "plugin.json"
    try:
        return str(json.loads(manifest.read_text()).get("version", "0"))
    except (OSError, ValueError):
        return "0"


def _result(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def handle_message(msg):
    """Route one JSON-RPC message. Returns a response dict, or None for notifications."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "invalid request")
    method, id_ = msg["method"], msg.get("id")
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        return _error(id_, -32602, "params must be an object")
    if method == "initialize":
        requested = params.get("protocolVersion")
        return _result(id_, {
            "protocolVersion": requested if requested in SUPPORTED_VERSIONS else LATEST_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "token-usage", "version": plugin_version()},
        })
    if method == "ping":
        return _result(id_, {})
    if method == "tools/list":
        return _result(id_, {"tools": TOOLS})
    if "id" not in msg:
        return None  # notification (initialized, cancelled, ...) — nothing to say
    return _error(id_, -32601, f"method not found: {method}")


def serve(stdin=None, stdout=None):
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = _error(None, -32700, "parse error")
        else:
            reply = handle_message(msg)
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()


if __name__ == "__main__":
    try:
        serve()
    except (KeyboardInterrupt, BrokenPipeError):
        pass
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `$PY -m pytest tests/test_mcp.py -q`
Expected: 8 passed.

- [ ] **Step 6: Commit**

```bash
git add scripts/mcp_server.py tests/conftest.py tests/test_mcp.py
git commit -m "feat: MCP server skeleton — stdio JSON-RPC loop, initialize/ping, protocol errors" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01PqaqeJS8JsAU1To3fvsPH5"
```

---

### Task 4: Tool definitions, argument validation, dispatch with error wrapping

**Files:**
- Modify: `scripts/mcp_server.py`
- Test: `tests/test_mcp.py`

**Interfaces:**
- Produces:
  - `TOOLS: list[dict]` — five entries `{"name", "description", "inputSchema"}`; every `inputSchema` has `"type": "object"`, `"properties"`, `"additionalProperties": False`, and `"required"` where applicable.
  - `validate_args(schema, args) -> list[str]` — every problem found (empty list = valid). Checks: args is an object; unknown keys; required keys; `string`/`boolean`/`integer`/`number` types (bools are not numbers); `enum`; integer `minimum`.
  - `class ToolError(Exception)` — handlers raise it for user-facing failures.
  - `call_tool(name, args) -> dict` — `{"content": [{"type": "text", "text": str}], "isError": bool}`. Unknown tool, validation problems, `ToolError`, `SystemExit` (from analyser `sys.exit` calls) and any other exception become `isError: True` results. While a handler runs, `sys.stdout` is redirected to `sys.stderr`.
  - `HANDLERS: dict[str, callable]` — name → `handler(args) -> str`. Empty until Tasks 5–6 register handlers; `tools/call` for a listed tool with no handler returns a tool error `not implemented`.
  - `handle_message` gains the `tools/call` branch: missing/non-string `params.name` → `-32602`; otherwise `_result(id_, call_tool(name, params.get("arguments") or {}))`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp.py`:

```python
TOOL_NAMES = {"session_cost", "history", "insights", "diff", "top_consumers"}


def test_tools_list_names_and_schema_shape(mcp):
    r = mcp.handle_message(req("tools/list"))
    tools = r["result"]["tools"]
    assert {t["name"] for t in tools} == TOOL_NAMES
    for t in tools:
        s = t["inputSchema"]
        assert t["description"]
        assert s["type"] == "object" and s["additionalProperties"] is False
        assert "format" in s["properties"]
        assert s["properties"]["format"]["enum"] == ["json", "markdown"]
    by_name = {t["name"]: t for t in tools}
    assert by_name["diff"]["inputSchema"]["required"] == ["old", "new"]
    assert by_name["history"]["inputSchema"]["properties"]["by"]["enum"] == \
        ["project", "day", "command", "model"]
    assert by_name["top_consumers"]["inputSchema"]["properties"]["limit"]["minimum"] == 1


def test_validate_args_reports_every_problem(mcp):
    schema = {"type": "object", "additionalProperties": False,
              "required": ["old"],
              "properties": {"old": {"type": "string"},
                             "limit": {"type": "integer", "minimum": 1},
                             "flag": {"type": "boolean"},
                             "budget": {"type": "number"},
                             "by": {"type": "string", "enum": ["a", "b"]}}}
    problems = mcp.validate_args(schema, {"limit": 0, "flag": "yes", "budget": True,
                                          "by": "zzz", "bogus": 1})
    joined = "; ".join(problems)
    for needle in ("old", "limit", "flag", "budget", "by", "bogus"):
        assert needle in joined, needle
    assert mcp.validate_args(schema, {"old": "x", "limit": 3, "budget": 2.5, "by": "a"}) == []
    assert mcp.validate_args(schema, "not an object") == ["arguments must be an object"]


def test_tools_call_requires_name(mcp):
    r = mcp.handle_message(req("tools/call", arguments={}))
    assert r["error"]["code"] == -32602


def test_unknown_tool_is_a_tool_error_not_protocol_error(mcp):
    r = mcp.handle_message(req("tools/call", name="nope", arguments={}))
    assert "error" not in r
    assert r["result"]["isError"] is True
    assert "unknown tool" in r["result"]["content"][0]["text"]


def test_invalid_arguments_are_a_tool_error(mcp):
    r = mcp.handle_message(req("tools/call", name="history", arguments={"by": "galaxy"}))
    res = r["result"]
    assert res["isError"] is True and "by" in res["content"][0]["text"]


def test_call_tool_wraps_exceptions_and_sys_exit(mcp, monkeypatch):
    mcp.HANDLERS["boom"] = lambda args: 1 / 0
    mcp.SCHEMAS["boom"] = {"type": "object", "properties": {}, "additionalProperties": False}
    r = mcp.call_tool("boom", {})
    assert r["isError"] and "ZeroDivisionError" in r["content"][0]["text"]

    def exits(args):
        import sys
        sys.exit("token-usage: invalid --since value")
    mcp.HANDLERS["exits"] = exits
    mcp.SCHEMAS["exits"] = mcp.SCHEMAS["boom"]
    r = mcp.call_tool("exits", {})
    assert r["isError"] and "invalid --since" in r["content"][0]["text"]


def test_handler_prints_never_reach_stdout(mcp, capsys):
    def chatty(args):
        print("progress…")          # analyser-style stderr chatter, but on stdout
        return "ok"
    mcp.HANDLERS["chatty"] = chatty
    mcp.SCHEMAS["chatty"] = {"type": "object", "properties": {}, "additionalProperties": False}
    r = mcp.call_tool("chatty", {})
    out, err = capsys.readouterr()
    assert r == {"content": [{"type": "text", "text": "ok"}], "isError": False}
    assert out == "" and "progress" in err
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$PY -m pytest tests/test_mcp.py -q`
Expected: the new tests fail (`tools/list` returns an empty list; `validate_args`, `HANDLERS`, `SCHEMAS`, `call_tool` do not exist; `tools/call` returns `-32601`).

- [ ] **Step 3: Implement schemas, validation and dispatch**

In `scripts/mcp_server.py`, replace `TOOLS = []  # filled in below ...` with:

```python
FORMAT = {"type": "string", "enum": ["json", "markdown"],
          "description": "json (default): structured data, same shapes as the CLI's json output. "
                         "markdown: the rendered table/text."}
SESSION_SELECTORS = {
    "transcript": {"type": "string", "description": "Path to a session .jsonl transcript."},
    "session_id": {"type": "string",
                   "description": "Claude Code session id; searched across every project."},
}
SINCE = {"type": "string", "description": "Window start: Nd (e.g. 7d) or YYYY-MM-DD."}
PROJECT = {"type": "string", "description": "Substring filter on the project slug."}


def _schema(properties, required=None):
    s = {"type": "object", "properties": dict(properties, format=FORMAT),
         "additionalProperties": False}
    if required:
        s["required"] = list(required)
    return s


TOOLS = [
    {"name": "session_cost",
     "description": "Per-activity token usage and estimated API cost for one Claude Code / "
                    "Cowork session: which slash commands, skills and subagents consumed what. "
                    "Defaults to the current project's newest session. Result names the "
                    "transcript analysed.",
     "inputSchema": _schema(dict(SESSION_SELECTORS,
                                 agents={"type": "boolean",
                                         "description": "Markdown: add per-agent-type rows."},
                                 models={"type": "boolean",
                                         "description": "Markdown: add per-model rows."}))},
    {"name": "history",
     "description": "Cross-session token usage and cost rolled up by project, day, command or "
                    "model, with optional window and project filters.",
     "inputSchema": _schema({"by": {"type": "string",
                                    "enum": ["project", "day", "command", "model"]},
                             "since": SINCE, "project": PROJECT})},
    {"name": "insights",
     "description": "Rule-based findings about token spend. Session mode (default) checks one "
                    "session against the project's 30-day norms; pass since for window mode "
                    "(spend trend, top mover). Pure arithmetic, no LLM.",
     "inputSchema": _schema(dict(SESSION_SELECTORS, since=SINCE, project=PROJECT,
                                 budget_usd={"type": "number",
                                             "description": "Session budget for the budget-pace "
                                                            "rule (overrides TOKEN_USAGE_BUDGET_USD)."}))},
    {"name": "diff",
     "description": "Compare two sessions label-by-label: per-activity cost and output deltas.",
     "inputSchema": _schema({"old": {"type": "string",
                                     "description": "Transcript path or session id (baseline)."},
                             "new": {"type": "string",
                                     "description": "Transcript path or session id (comparison)."}},
                            required=["old", "new"])},
    {"name": "top_consumers",
     "description": "The costliest sessions or command labels in a window.",
     "inputSchema": _schema({"by": {"type": "string", "enum": ["session", "command"]},
                             "since": SINCE, "project": PROJECT,
                             "limit": {"type": "integer", "minimum": 1,
                                       "description": "Rows to return (default 10)."}})},
]
SCHEMAS = {t["name"]: t["inputSchema"] for t in TOOLS}
HANDLERS = {}  # name -> handler(args) -> str; registered further down


class ToolError(Exception):
    """A user-facing tool failure (reported as isError, never a protocol error)."""


_TYPES = {"string": str, "boolean": bool, "integer": int, "number": (int, float)}


def validate_args(schema, args):
    """Every problem with `args` against a tool inputSchema; [] when valid."""
    if not isinstance(args, dict):
        return ["arguments must be an object"]
    problems = []
    props = schema.get("properties", {})
    for key in args:
        if key not in props:
            problems.append(f"unknown argument {key!r}")
    for key in schema.get("required", []):
        if key not in args:
            problems.append(f"missing required argument {key!r}")
    for key, spec in props.items():
        if key not in args:
            continue
        val, typ = args[key], spec.get("type")
        ok = isinstance(val, _TYPES[typ]) and not (typ in ("integer", "number") and isinstance(val, bool))
        if not ok:
            problems.append(f"{key} must be a {typ}")
            continue
        if "enum" in spec and val not in spec["enum"]:
            problems.append(f"{key} must be one of {spec['enum']}")
        if "minimum" in spec and val < spec["minimum"]:
            problems.append(f"{key} must be >= {spec['minimum']}")
    return problems


def _tool_result(text, is_error=False):
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def call_tool(name, args):
    handler = HANDLERS.get(name)
    if name not in SCHEMAS:
        return _tool_result(f"unknown tool: {name}", True)
    problems = validate_args(SCHEMAS[name], args)
    if problems:
        return _tool_result("invalid arguments: " + "; ".join(problems), True)
    if handler is None:
        return _tool_result(f"{name}: not implemented", True)
    real_stdout = sys.stdout
    sys.stdout = sys.stderr  # analyser progress/prints must never corrupt the JSON-RPC stream
    try:
        return _tool_result(handler(args))
    except ToolError as e:
        return _tool_result(str(e), True)
    except SystemExit as e:  # analyser helpers call sys.exit() with a message
        return _tool_result(str(e.code), True)
    except Exception as e:  # noqa: BLE001 — a tool must not take the server down
        return _tool_result(f"{type(e).__name__}: {e}", True)
    finally:
        sys.stdout = real_stdout
```

Then in `handle_message`, after the `tools/list` branch, add:

```python
    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str) or not name:
            return _error(id_, -32602, "tools/call requires params.name")
        return _result(id_, call_tool(name, params.get("arguments") or {}))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `$PY -m pytest tests/test_mcp.py -q`
Expected: all pass (15).

- [ ] **Step 5: Commit**

```bash
git add scripts/mcp_server.py tests/test_mcp.py
git commit -m "feat: MCP tool schemas, argument validation, dispatch with isError wrapping" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01PqaqeJS8JsAU1To3fvsPH5"
```

---

### Task 5: `session_cost` and `diff` handlers

**Files:**
- Modify: `scripts/mcp_server.py`
- Test: `tests/test_mcp.py`

**Interfaces:**
- Consumes: `tu.locate_transcript(arg, session_id, project_dir)`, `tu.parse_session`, `tu.aggregate`, `tu.load_pricing`, `tu.render_report(data, show_agents, show_models)`, `tu.diff_data(old, new, pricing)`, `tu.render_diff`.
- Produces:
  - `pick_transcript(path=None, session_id=None) -> Path` — raises `ToolError` naming what was searched. Reads `TOKEN_USAGE_PROJECT_DIR` for the project.
  - `finish(data, render, fmt) -> str` — `render(data)` when `fmt == "markdown"`, else compact JSON.
  - `tool_session_cost(args) -> str`, `tool_diff(args) -> str`, registered in `HANDLERS`.
  - `session_cost` JSON carries `"transcript": "<path>"` (same key name as the CLI's `transcript_path`? No — the CLI uses `transcript_path`; the tool uses **`transcript`** per the spec).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp.py`:

```python
import os
from conftest import assistant, usage, user, write_jsonl


def seed(tmp_path, monkeypatch):
    proj = tmp_path / "projects"
    s1 = write_jsonl(proj / "-Users-x-alpha" / "aaa-111.jsonl", [
        user("2026-06-10T10:00:00Z", command="/review"),
        assistant("2026-06-10T10:00:01Z", usage(out=100_000), request_id="r1"),
    ])
    s2 = write_jsonl(proj / "-Users-x-beta" / "bbb-222.jsonl", [
        user("2026-06-12T10:00:00Z", command="/review"),
        assistant("2026-06-12T10:00:01Z", usage(out=40_000), request_id="r2"),
        user("2026-06-12T10:02:00Z", command="/commit"),
        assistant("2026-06-12T10:02:01Z", usage(out=20_000), request_id="r3"),
    ])
    os.utime(s1, (1_700_000_000, 1_700_000_000))
    os.utime(s2, (1_700_000_100, 1_700_000_100))
    monkeypatch.setenv("TOKEN_USAGE_PROJECTS_DIR", str(proj))
    monkeypatch.setenv("TOKEN_USAGE_LEDGER_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("TOKEN_USAGE_TRANSCRIPT", raising=False)
    monkeypatch.delenv("TOKEN_USAGE_PROJECT_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    return proj, s1, s2


def call(mcp, name, **arguments):
    r = mcp.handle_message(req("tools/call", name=name, arguments=arguments))
    res = r["result"]
    return res["content"][0]["text"], res["isError"]


def test_session_cost_json_default_names_transcript(mcp, tmp_path, monkeypatch):
    proj, s1, s2 = seed(tmp_path, monkeypatch)
    text, err = call(mcp, "session_cost", transcript=str(s1))
    assert not err
    data = json.loads(text)
    assert data["transcript"] == str(s1)
    assert data["by_label"]["/review"]["usage"]["output"] == 100_000
    assert data["total"]["cost_usd"] == 5.0        # 100k out @ $50/MTok


def test_session_cost_by_session_id_and_markdown(mcp, tmp_path, monkeypatch):
    proj, s1, s2 = seed(tmp_path, monkeypatch)
    text, err = call(mcp, "session_cost", session_id="bbb-222", format="markdown", models=True)
    assert not err
    assert text.startswith("| Activity |")
    assert "`/commit`" in text and "↳ claude-fable-5" in text


def test_session_cost_uses_project_dir_env_then_newest_anywhere(mcp, tmp_path, monkeypatch):
    proj, s1, s2 = seed(tmp_path, monkeypatch)
    monkeypatch.setenv("TOKEN_USAGE_PROJECT_DIR", "/Users/x/alpha")
    assert json.loads(call(mcp, "session_cost")[0])["transcript"] == str(s1)
    monkeypatch.delenv("TOKEN_USAGE_PROJECT_DIR")
    assert json.loads(call(mcp, "session_cost")[0])["transcript"] == str(s2)


def test_session_cost_not_found_is_a_tool_error(mcp, tmp_path, monkeypatch):
    proj, s1, s2 = seed(tmp_path, monkeypatch)
    text, err = call(mcp, "session_cost", session_id="zzz-000")
    assert err and "zzz-000" in text and str(proj) in text
    text, err = call(mcp, "session_cost", transcript=str(tmp_path / "gone.jsonl"))
    assert err and "gone.jsonl" in text


def test_diff_by_path_and_id(mcp, tmp_path, monkeypatch):
    proj, s1, s2 = seed(tmp_path, monkeypatch)
    data = json.loads(call(mcp, "diff", old=str(s1), new="bbb-222")[0])
    review = next(r for r in data["rows"] if r["label"] == "/review")
    assert review["delta_output"] == -60_000
    md, err = call(mcp, "diff", old="aaa-111", new="bbb-222", format="markdown")
    assert not err and md.startswith("| Activity | A cost | B cost |")
    text, err = call(mcp, "diff", old="aaa-111", new="missing-1")
    assert err and "missing-1" in text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$PY -m pytest tests/test_mcp.py -q -k "session_cost or diff"`
Expected: fail with `isError` true and text `session_cost: not implemented` / `diff: not implemented`.

- [ ] **Step 3: Implement the handlers**

Append to `scripts/mcp_server.py` (after `call_tool`):

```python
# --- tool handlers -----------------------------------------------------------

def pick_transcript(path=None, session_id=None):
    """Resolve a session per the spec order, or raise ToolError saying what was tried."""
    project_dir = os.environ.get("TOKEN_USAGE_PROJECT_DIR") or None
    t = tu.locate_transcript(path, session_id=session_id, project_dir=project_dir)
    if t:
        return t
    if path:
        raise ToolError(f"transcript not found: {path}")
    if session_id:
        raise ToolError(f"no transcript for session id {session_id!r} under {tu.projects_dir()}")
    where = f"{tu.projects_dir()}" + (f" (project dir {project_dir})" if project_dir else "")
    raise ToolError(f"no transcript found under {where}; pass transcript or session_id")


def finish(data, render, fmt):
    return render(data) if fmt == "markdown" else json.dumps(data)


def tool_session_cost(args):
    t = pick_transcript(args.get("transcript"), args.get("session_id"))
    data = tu.aggregate(tu.parse_session(t), tu.load_pricing())
    data["transcript"] = str(t)
    return finish(data,
                  lambda d: tu.render_report(d, show_agents=bool(args.get("agents")),
                                             show_models=bool(args.get("models"))),
                  args.get("format"))


def _path_or_id(value):
    """diff accepts either form per side: an existing path wins, else a session id."""
    p = Path(value)
    return pick_transcript(path=value) if p.is_file() else pick_transcript(session_id=value)


def tool_diff(args):
    old, new = _path_or_id(args["old"]), _path_or_id(args["new"])
    data = tu.diff_data(old, new, tu.load_pricing())
    return finish(data, tu.render_diff, args.get("format"))


HANDLERS.update({"session_cost": tool_session_cost, "diff": tool_diff})
```

- [ ] **Step 4: Run the suite**

Run: `$PY -m pytest tests/ -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/mcp_server.py tests/test_mcp.py
git commit -m "feat: MCP session_cost and diff tools" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01PqaqeJS8JsAU1To3fvsPH5"
```

---

### Task 6: `history`, `insights` and `top_consumers` handlers

**Files:**
- Modify: `scripts/mcp_server.py`
- Test: `tests/test_mcp.py`

**Interfaces:**
- Consumes: `tu.run_history(by, since, project)`, `tu.render_history`, `tu.run_insights(transcript, since, project, budget)`, `tu.render_insights`, `tu.run_top_consumers(by, since, project, limit)`, `tu.render_top_consumers`, `pick_transcript`, `finish`.
- Produces: `tool_history`, `tool_insights`, `tool_top_consumers` registered in `HANDLERS`. `insights` in session mode adds `"transcript"` to the JSON. `insights` with both a session selector and `since` is a `ToolError`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp.py`:

```python
def test_history_json_matches_cli_and_markdown_renders(mcp, tu, tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    data = json.loads(call(mcp, "history", by="command", since="2026-01-01")[0])
    assert data == tu.run_history(by="command", since="2026-01-01")
    md, err = call(mcp, "history", by="project", format="markdown")
    assert not err and md.startswith("| Project | Calls |")


def test_history_bad_since_is_a_tool_error(mcp, tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    text, err = call(mcp, "history", since="last tuesday")
    assert err and "invalid --since" in text


def test_insights_session_mode_names_transcript(mcp, tmp_path, monkeypatch):
    proj, s1, s2 = seed(tmp_path, monkeypatch)
    data = json.loads(call(mcp, "insights", session_id="bbb-222", budget_usd=1.0)[0])
    assert data["mode"] == "session" and data["transcript"] == str(s2)
    assert isinstance(data["findings"], list)
    md, err = call(mcp, "insights", session_id="bbb-222", format="markdown")
    assert not err and (md == "No notable findings." or md.startswith("- ["))


def test_insights_window_mode(mcp, tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    data = json.loads(call(mcp, "insights", since="2026-01-01")[0])
    assert data["mode"] == "window" and data["baseline"]["sessions"] == 2
    text, err = call(mcp, "insights", since="7d", session_id="bbb-222")
    assert err and "not both" in text


def test_top_consumers_tool(mcp, tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    data = json.loads(call(mcp, "top_consumers", since="2026-01-01", limit=1)[0])
    assert [r["session_id"] for r in data["rows"]] == ["aaa-111"]
    md, err = call(mcp, "top_consumers", by="command", since="2026-01-01", format="markdown")
    assert not err and md.startswith("| Command | Sessions |")
    text, err = call(mcp, "top_consumers", limit=0)
    assert err and "limit" in text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$PY -m pytest tests/test_mcp.py -q -k "history or insights or top_consumers"`
Expected: `not implemented` tool errors (and `limit=0` already passes validation-wise as an error — fine).

- [ ] **Step 3: Implement the handlers**

Append to `scripts/mcp_server.py` (before the final `HANDLERS.update(...)` line, then extend that call):

```python
def tool_history(args):
    data = tu.run_history(by=args.get("by", "project"), since=args.get("since"),
                          project=args.get("project"))
    return finish(data, tu.render_history, args.get("format"))


def tool_insights(args):
    has_session = bool(args.get("transcript") or args.get("session_id"))
    if has_session and args.get("since"):
        raise ToolError("pass a transcript/session_id OR since, not both")
    budget = args.get("budget_usd")
    if budget is None:
        try:
            budget = float(os.environ["TOKEN_USAGE_BUDGET_USD"])
        except (KeyError, ValueError):
            budget = None
    if args.get("since"):
        data = tu.run_insights(since=args["since"], project=args.get("project"), budget=budget)
    else:
        t = pick_transcript(args.get("transcript"), args.get("session_id"))
        data = tu.run_insights(transcript=str(t), budget=budget)
        data["transcript"] = str(t)
    return finish(data, tu.render_insights, args.get("format"))


def tool_top_consumers(args):
    data = tu.run_top_consumers(by=args.get("by", "session"), since=args.get("since", "30d"),
                                project=args.get("project"), limit=args.get("limit", 10))
    return finish(data, tu.render_top_consumers, args.get("format"))
```

and change the registration line to:

```python
HANDLERS.update({"session_cost": tool_session_cost, "diff": tool_diff,
                 "history": tool_history, "insights": tool_insights,
                 "top_consumers": tool_top_consumers})
```

- [ ] **Step 4: Run the suite**

Run: `$PY -m pytest tests/ -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/mcp_server.py tests/test_mcp.py
git commit -m "feat: MCP history, insights and top_consumers tools" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01PqaqeJS8JsAU1To3fvsPH5"
```

---

### Task 7: Plugin registration and end-to-end subprocess test

**Files:**
- Create: `.mcp.json`
- Test: `tests/test_mcp.py`

**Interfaces:**
- Produces: `.mcp.json` registering server `token-usage` → `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/mcp_server.py` with `TOKEN_USAGE_PROJECT_DIR=${CLAUDE_PROJECT_DIR}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp.py`:

```python
import subprocess
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "scripts" / "mcp_server.py"
PLUGIN_ROOT = SERVER.parent.parent


def test_mcp_json_registers_server_with_plugin_root_paths():
    cfg = json.loads((PLUGIN_ROOT / ".mcp.json").read_text())
    srv = cfg["mcpServers"]["token-usage"]
    assert srv["command"] == "python3"
    assert srv["args"] == ["${CLAUDE_PLUGIN_ROOT}/scripts/mcp_server.py"]
    assert srv["env"] == {"TOKEN_USAGE_PROJECT_DIR": "${CLAUDE_PROJECT_DIR}"}


def test_end_to_end_over_pipes(tmp_path, monkeypatch):
    proj, s1, s2 = seed(tmp_path, monkeypatch)
    env = dict(os.environ, TOKEN_USAGE_PROJECTS_DIR=str(proj),
               TOKEN_USAGE_LEDGER_DIR=str(tmp_path / "cache"),
               TOKEN_USAGE_PROJECT_DIR="/Users/x/beta")
    script = "\n".join(json.dumps(m) for m in [
        req("initialize", id_=1, protocolVersion="2025-06-18", capabilities={},
            clientInfo={"name": "pytest", "version": "0"}),
        notif("notifications/initialized"),
        req("tools/list", id_=2),
        req("tools/call", id_=3, name="session_cost", arguments={}),
        req("tools/call", id_=4, name="top_consumers",
            arguments={"since": "2026-01-01", "format": "markdown"}),
    ]) + "\n"
    r = subprocess.run([sys.executable, str(SERVER)], input=script, capture_output=True,
                       text=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    assert r.stderr == ""
    replies = {m["id"]: m for m in (json.loads(l) for l in r.stdout.splitlines())}
    assert set(replies) == {1, 2, 3, 4}
    assert replies[1]["result"]["protocolVersion"] == "2025-06-18"
    assert {t["name"] for t in replies[2]["result"]["tools"]} == TOOL_NAMES
    cost = json.loads(replies[3]["result"]["content"][0]["text"])
    assert cost["transcript"] == str(s2)                # TOKEN_USAGE_PROJECT_DIR honoured
    assert replies[4]["result"]["content"][0]["text"].startswith("| Session | Project |")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$PY -m pytest tests/test_mcp.py -q -k "mcp_json or end_to_end"`
Expected: `FileNotFoundError` for `.mcp.json`; the pipe test may already pass (the server is complete) — if it does, that is fine: it is an integration check, and the `.mcp.json` test is the red one.

- [ ] **Step 3: Create `.mcp.json`**

```json
{
  "mcpServers": {
    "token-usage": {
      "command": "python3",
      "args": ["${CLAUDE_PLUGIN_ROOT}/scripts/mcp_server.py"],
      "env": {"TOKEN_USAGE_PROJECT_DIR": "${CLAUDE_PROJECT_DIR}"}
    }
  }
}
```

- [ ] **Step 4: Run the suite, then a manual smoke against the real plugin**

Run: `$PY -m pytest tests/ -q` — expected all pass.

Then, from the repo root:

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}' '{"jsonrpc":"2.0","method":"notifications/initialized"}' '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"top_consumers","arguments":{"since":"7d","limit":3,"format":"markdown"}}}' | TOKEN_USAGE_PROJECT_DIR="$PWD" python3 scripts/mcp_server.py
```

Expected: two JSON lines on stdout; the second contains a markdown table of the three costliest sessions in the last week; nothing on stderr except possible "parsed N transcripts…" progress (which is stderr, acceptable).

- [ ] **Step 5: Commit**

```bash
git add .mcp.json tests/test_mcp.py
git commit -m "feat: register token-usage MCP server in the plugin; end-to-end pipe test" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01PqaqeJS8JsAU1To3fvsPH5"
```

---

### Task 8: Docs and 0.6.0 release metadata

**Files:**
- Modify: `README.md` (Features list; new "MCP server" section after "Statusline (optional)"; CLI usage block gains `top_consumers`)
- Modify: `skills/report/SKILL.md` (frontmatter `version: 0.6.0`; new "Prefer the MCP tools when present" paragraph; CLI list gains `top_consumers`)
- Modify: `CHANGELOG.md` (`[0.6.0]` section, link refs)
- Modify: `.claude-plugin/plugin.json` (`"version": "0.6.0"`, description mentions MCP)
- Test: existing `test_server_version_matches_plugin_manifest` keeps manifest and server in step.

- [ ] **Step 1: README — Features bullet**

Add after the "User pricing overlay" bullet:

```markdown
- **MCP server** — a bundled stdio MCP server (`scripts/mcp_server.py`, stdlib only)
  exposes `session_cost`, `history`, `insights`, `diff` and `top_consumers` as tools.
  Claude Code starts it automatically with the plugin; Claude desktop can register the
  same script. JSON by default, `format: "markdown"` for the rendered tables.
- **Top consumers** — `top_consumers --by session|command` lists the costliest sessions
  or command labels in a window, the question `history` could not answer directly.
```

- [ ] **Step 2: README — CLI usage block**

In the `### CLI (outside Claude Code)` code block, after the `insights` examples add:

```bash
# Costliest sessions (or --by command) in the last 30 days
python3 scripts/token_usage.py top_consumers --since 30d --limit 10
python3 scripts/token_usage.py top_consumers --by command --project my-repo --json
```

- [ ] **Step 3: README — MCP server section**

Insert after the `### Statusline (optional)` subsection (before `## Insights`):

````markdown
### MCP server

The plugin ships a stdio MCP server (`.mcp.json` → `scripts/mcp_server.py`, stdlib only,
no install). When the plugin is enabled, Claude Code starts it and the tools appear as
`mcp__plugin_token-usage_token-usage__<tool>`:

| Tool | What it answers |
|---|---|
| `session_cost` | Per-activity breakdown of one session (`transcript` or `session_id`; defaults to the current project's newest session). Result names the transcript analysed. |
| `history` | Cross-session rollup `by` project / day / command / model, with `since` and `project` filters. |
| `insights` | Rule-based findings: session mode (one session vs the project's 30-day norms) or window mode (`since`). Optional `budget_usd`. |
| `diff` | Per-activity cost and output deltas between two sessions (paths or session ids). |
| `top_consumers` | Costliest sessions or command labels in a window (`by`, `since`, `project`, `limit`). |

Every tool takes `format`: `json` (default, same shapes as the CLI's JSON output) or
`markdown` (the rendered table). Failures come back as tool results with `isError`, never
as protocol errors, so a missing transcript or a bad `since` is a readable message.

**"Current session"** resolves in this order: explicit `transcript` path → `session_id`
(searched across every project) → newest transcript for `TOKEN_USAGE_PROJECT_DIR`
(Claude Code passes `${CLAUDE_PROJECT_DIR}`) → the Cowork mount → newest transcript on
the machine (Claude desktop, which has no project dir).

**Claude desktop / Cowork.** Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "token-usage": {
      "command": "python3",
      "args": ["/absolute/path/to/token-usage/scripts/mcp_server.py"]
    }
  }
}
```

or, for a Claude Code user scope outside the plugin:

```bash
claude mcp add --scope user token-usage -- python3 /absolute/path/to/token-usage/scripts/mcp_server.py
```

The server reads `~/.claude/projects` on the host, so a desktop session sees the same
history the CLI does. No caching in-process: pricing overlay edits apply on the next call.
````

- [ ] **Step 4: SKILL.md**

Change the frontmatter `version: 0.5.0` to `version: 0.6.0`. Add, directly under the `# token-usage report` heading and its first paragraph:

```markdown
## Prefer the MCP tools when present

If tools named `mcp__plugin_token-usage_token-usage__session_cost`, `…__history`,
`…__insights`, `…__diff` or `…__top_consumers` are available in this session, call them
instead of shelling out — same data, structured result, no path resolution needed. Use
`format: "markdown"` when the user wants the table shown verbatim. Fall back to the CLI
below when the tools are absent (e.g. a Cowork sandbox without the server registered).
```

In the `## How to run` code block add after the insights lines:

```bash
# Costliest sessions or commands in a window
python3 "<plugin-root>/scripts/token_usage.py" top_consumers [--by session|command] [--since 30d] [--limit N]
```

- [ ] **Step 5: CHANGELOG and plugin manifest**

In `CHANGELOG.md`, rename the current `## [Unreleased]` block's content into a new
section `## [0.6.0] — <today's date>` (keep an empty `## [Unreleased]` above it) and
prepend to its `### Added`:

```markdown
- **MCP server** — `scripts/mcp_server.py`, a stdlib stdio JSON-RPC 2.0 server
  registered by the plugin's new `.mcp.json` (Claude Code auto-starts it; Claude
  desktop can register the same script). Tools: `session_cost`, `history`,
  `insights`, `diff`, `top_consumers`; `format: json|markdown`; tool failures
  are `isError` results, protocol problems are JSON-RPC errors; nothing but
  JSON-RPC reaches stdout. Current session resolves from
  `TOKEN_USAGE_PROJECT_DIR` (`${CLAUDE_PROJECT_DIR}`), then the Cowork mount,
  then the newest transcript anywhere.
- **`top_consumers` subcommand** — costliest sessions (`--by session`) or
  command labels aggregated across sessions (`--by command`) in a window;
  `--since`, `--project`, `--limit`, `--json`. Unpriced rows sort last.
- **Session-id lookup** — `locate_transcript()` resolves a Claude Code session
  id across every project; `resolve_transcript()` now fails cleanly on a
  non-existent explicit path instead of crashing in the parser.
```

Add the compare link at the bottom:

```markdown
[Unreleased]: https://github.com/WizzoUK2/token-usage/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/WizzoUK2/token-usage/compare/v0.5.0...v0.6.0
```

(and repoint the existing `[Unreleased]` line to `v0.6.0...HEAD`.)

In `.claude-plugin/plugin.json` set `"version": "0.6.0"` and append `, and a bundled MCP server` to the description sentence before the final period.

- [ ] **Step 6: Run the full suite**

Run: `$PY -m pytest tests/ -q`
Expected: all pass, including `test_server_version_matches_plugin_manifest` now asserting `0.6.0`.

- [ ] **Step 7: Commit**

```bash
git add README.md skills/report/SKILL.md CHANGELOG.md .claude-plugin/plugin.json
git commit -m "docs: 0.6.0 — MCP server, top_consumers, session-id lookup" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01PqaqeJS8JsAU1To3fvsPH5"
```

---

## Self-review

**Spec coverage.** §1 protocol subset → Task 3 (initialize/initialized/ping/unknown/parse) + Task 4 (tools/list, tools/call). §1 registration → Task 7. §2 five tools with schemas and `format` → Tasks 4–6; `top_consumers` analyser + CLI → Task 2; session resolution → Task 1 (+ order amendment). §3 dispatch/validation/no caching → Task 4. §4 every error case → Tasks 3–6 (tests name each). §5 tests → each task; subprocess smoke → Task 7; 3.9 compatibility → no f-string `=`, no `match`, no union annotations anywhere above. §6 docs/release → Task 8.

**Placeholders.** None: every step carries the code or text it asks for.

**Type consistency.** `locate_transcript(arg=None, session_id=None, project_dir=None)` (Task 1) is called with those keywords in Task 5. `run_top_consumers(by, since, project, limit)` (Task 2) matches Task 6. `call_tool`, `HANDLERS`, `SCHEMAS`, `ToolError`, `_tool_result`, `finish`, `pick_transcript` are defined once (Tasks 4–5) and used with the same names afterwards. The JSON key the tools add is `transcript` everywhere (the CLI's `transcript_path` is untouched).
