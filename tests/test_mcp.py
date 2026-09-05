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
    # Markdown must name the transcript analysed (render_report reads
    # transcript_path, not the tool's own "transcript" key).
    assert f"Session: `{s2.parent.name}/{s2.name}`" in text


def test_session_cost_rejects_blank_selectors(mcp, tmp_path, monkeypatch):
    proj, s1, s2 = seed(tmp_path, monkeypatch)
    text, err = call(mcp, "session_cost", transcript="")
    assert err and "blank" in text
    text, err = call(mcp, "session_cost", session_id="")
    assert err and "blank" in text
    text, err = call(mcp, "session_cost", session_id="   ")
    assert err and "blank" in text


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


def test_diff_rejects_blank_selectors(mcp, tmp_path, monkeypatch):
    proj, s1, s2 = seed(tmp_path, monkeypatch)
    text, err = call(mcp, "diff", old="", new="bbb-222")
    assert err and "blank" in text
    text, err = call(mcp, "diff", old=str(s1), new="   ")
    assert err and "blank" in text


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


def test_history_default_by_is_project(mcp, tmp_path, monkeypatch):
    # No `by` passed -> spec default is "project", not "day" or anything else.
    seed(tmp_path, monkeypatch)
    data = json.loads(call(mcp, "history", since="2026-01-01")[0])
    assert data["by"] == "project"
    assert {r["key"] for r in data["rows"]} == {"-Users-x-alpha", "-Users-x-beta"}


def test_history_project_filter_is_passed_through(mcp, tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    data = json.loads(call(mcp, "history", since="2026-01-01", project="alpha")[0])
    assert data["project"] == "alpha"
    assert [r["key"] for r in data["rows"]] == ["-Users-x-alpha"]


def test_insights_window_mode_project_filter_is_passed_through(mcp, tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    data = json.loads(call(mcp, "insights", since="2026-01-01", project="alpha")[0])
    assert data["mode"] == "window" and data["baseline"]["sessions"] == 1


def test_top_consumers_project_filter_is_passed_through(mcp, tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    data = json.loads(call(mcp, "top_consumers", since="2026-01-01", project="beta")[0])
    assert data["project"] == "beta"
    assert [r["session_id"] for r in data["rows"]] == ["bbb-222"]


def test_top_consumers_default_since_is_30d(mcp, tmp_path, monkeypatch):
    # Seeded sessions are dated 2026-06 which, relative to the real clock this
    # suite runs under, is well outside a genuine rolling 30-day window. If the
    # default silently regressed to None (no filtering) these rows would show
    # up instead of being filtered out.
    seed(tmp_path, monkeypatch)
    data = json.loads(call(mcp, "top_consumers")[0])
    assert data["since"] == "30d"
    assert data["rows"] == []


def test_top_consumers_default_limit_is_10(mcp, tmp_path, monkeypatch):
    proj, s1, s2 = seed(tmp_path, monkeypatch)
    # Two more sessions so a limit=10 default is distinguishable from a
    # regression to some smaller number (e.g. 3) -- with only 2 rows both
    # limits would look identical.
    s3 = write_jsonl(proj / "-Users-x-gamma" / "ccc-333.jsonl", [
        user("2026-06-13T10:00:00Z", command="/review"),
        assistant("2026-06-13T10:00:01Z", usage(out=5_000), request_id="r4"),
    ])
    s4 = write_jsonl(proj / "-Users-x-delta" / "ddd-444.jsonl", [
        user("2026-06-14T10:00:00Z", command="/review"),
        assistant("2026-06-14T10:00:01Z", usage(out=5_000), request_id="r5"),
    ])
    os.utime(s3, (1_700_000_200, 1_700_000_200))
    os.utime(s4, (1_700_000_300, 1_700_000_300))
    data = json.loads(call(mcp, "top_consumers", since="2026-01-01")[0])
    assert data["limit"] == 10
    assert len(data["rows"]) == 4


def test_insights_session_mode_budget_usd_is_passed_through(mcp, tu, tmp_path, monkeypatch):
    proj, s1, s2 = seed(tmp_path, monkeypatch)
    monkeypatch.delenv("TOKEN_USAGE_BUDGET_USD", raising=False)
    baseline_cost = tu.aggregate(tu.parse_session(s2), tu.load_pricing())["total"]["cost_usd"]
    # A budget that puts this session's cost at 80% of budget -- inside the
    # [0.75, 1.0) "budget-pace" window -- so the finding only appears when
    # budget_usd actually reaches run_insights/session_insights.
    budget = baseline_cost / 0.8
    data = json.loads(call(mcp, "insights", session_id="bbb-222", budget_usd=budget)[0])
    assert any(f["rule"] == "budget-pace" for f in data["findings"])
    data_no_budget = json.loads(call(mcp, "insights", session_id="bbb-222")[0])
    assert not any(f["rule"] == "budget-pace" for f in data_no_budget["findings"])


def test_insights_session_mode_falls_back_to_budget_env_var(mcp, tu, tmp_path, monkeypatch):
    proj, s1, s2 = seed(tmp_path, monkeypatch)
    baseline_cost = tu.aggregate(tu.parse_session(s2), tu.load_pricing())["total"]["cost_usd"]
    budget = baseline_cost / 0.8
    monkeypatch.setenv("TOKEN_USAGE_BUDGET_USD", str(budget))
    data = json.loads(call(mcp, "insights", session_id="bbb-222")[0])
    assert any(f["rule"] == "budget-pace" for f in data["findings"])


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
