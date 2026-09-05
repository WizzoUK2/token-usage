"""MCP server: JSON-RPC framing, handshake, tools, error paths."""
import io
import json
import os
import subprocess
import sys

from conftest import SERVER, TOKEN_USAGE, assistant, usage, user, write_jsonl

PLUGIN_ROOT = SERVER.parent.parent


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


def test_id_less_requests_are_notifications_even_for_known_methods(mcp):
    # JSON-RPC 2.0 forbids replying to a notification, so an id-less message
    # gets no response whatever its method -- answering with "id": null is a
    # protocol violation, not a courtesy.
    for m in (notif("ping"), notif("initialize", protocolVersion="2025-06-18"),
              notif("tools/list"), notif("tools/call", name="history", arguments={})):
        assert mcp.handle_message(m) is None, m["method"]


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


def test_serve_survives_a_crash_in_handle_message(mcp, monkeypatch):
    # Anything escaping handle_message must become a JSON-RPC internal error
    # for that one request; the read loop keeps serving the next message.
    real = mcp.handle_message

    def boom(msg):
        if msg.get("method") == "explode":
            raise RuntimeError("kaboom")
        return real(msg)

    monkeypatch.setattr(mcp, "handle_message", boom)
    stdin = io.StringIO("\n".join([json.dumps(req("explode", id_=5)),
                                   json.dumps(req("ping", id_=6))]) + "\n")
    stdout = io.StringIO()
    mcp.serve(stdin=stdin, stdout=stdout)
    lines = [json.loads(l) for l in stdout.getvalue().splitlines()]
    assert lines[0]["id"] == 5 and lines[0]["error"]["code"] == -32603
    assert "kaboom" in lines[0]["error"]["message"]
    assert lines[1] == {"jsonrpc": "2.0", "id": 6, "result": {}}


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
    # top_consumers is the one tool with a non-empty default window; a caller
    # reading only the schema must be able to see it.
    assert "30d" in by_name["top_consumers"]["inputSchema"]["properties"]["since"]["description"]


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


def test_call_tool_wraps_exceptions_and_sys_exit(mcp):
    mcp.HANDLERS["boom"] = lambda args: 1 / 0
    mcp.SCHEMAS["boom"] = {"type": "object", "properties": {}, "additionalProperties": False}
    r = mcp.call_tool("boom", {})
    assert r["isError"] and "ZeroDivisionError" in r["content"][0]["text"]

    def exits(args):
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
    _proj, s1, _s2 = seed(tmp_path, monkeypatch)
    text, err = call(mcp, "session_cost", transcript=str(s1))
    assert not err
    data = json.loads(text)
    assert data["transcript"] == str(s1)
    assert data["by_label"]["/review"]["usage"]["output"] == 100_000
    assert data["total"]["cost_usd"] == 5.0        # 100k out @ $50/MTok


def test_session_cost_by_session_id_and_markdown(mcp, tmp_path, monkeypatch):
    _proj, _s1, s2 = seed(tmp_path, monkeypatch)
    text, err = call(mcp, "session_cost", session_id="bbb-222", format="markdown", models=True)
    assert not err
    assert text.startswith("| Activity |")
    assert "`/commit`" in text and "↳ claude-fable-5" in text
    # Markdown must name the transcript analysed (render_report reads
    # transcript_path, not the tool's own "transcript" key).
    assert f"Session: `{s2.parent.name}/{s2.name}`" in text


def test_session_cost_rejects_blank_selectors(mcp, tmp_path, monkeypatch):
    _proj, _s1, _s2 = seed(tmp_path, monkeypatch)
    text, err = call(mcp, "session_cost", transcript="")
    assert err and "blank" in text
    text, err = call(mcp, "session_cost", session_id="")
    assert err and "blank" in text
    text, err = call(mcp, "session_cost", session_id="   ")
    assert err and "blank" in text


def test_session_cost_uses_project_dir_env_then_newest_anywhere(mcp, tmp_path, monkeypatch):
    _proj, s1, s2 = seed(tmp_path, monkeypatch)
    monkeypatch.setenv("TOKEN_USAGE_PROJECT_DIR", "/Users/x/alpha")
    assert json.loads(call(mcp, "session_cost")[0])["transcript"] == str(s1)
    monkeypatch.delenv("TOKEN_USAGE_PROJECT_DIR")
    assert json.loads(call(mcp, "session_cost")[0])["transcript"] == str(s2)


def test_session_cost_not_found_is_a_tool_error(mcp, tmp_path, monkeypatch):
    proj, _s1, _s2 = seed(tmp_path, monkeypatch)
    text, err = call(mcp, "session_cost", session_id="zzz-000")
    assert err and "zzz-000" in text and str(proj) in text
    text, err = call(mcp, "session_cost", transcript=str(tmp_path / "gone.jsonl"))
    assert err and "gone.jsonl" in text


def test_diff_by_path_and_id(mcp, tmp_path, monkeypatch):
    _proj, s1, _s2 = seed(tmp_path, monkeypatch)
    data = json.loads(call(mcp, "diff", old=str(s1), new="bbb-222")[0])
    review = next(r for r in data["rows"] if r["label"] == "/review")
    assert review["delta_output"] == -60_000
    md, err = call(mcp, "diff", old="aaa-111", new="bbb-222", format="markdown")
    assert not err and md.startswith("| Activity | A cost | B cost |")
    text, err = call(mcp, "diff", old="aaa-111", new="missing-1")
    assert err and "missing-1" in text


def test_diff_rejects_blank_selectors(mcp, tmp_path, monkeypatch):
    _proj, s1, _s2 = seed(tmp_path, monkeypatch)
    text, err = call(mcp, "diff", old="", new="bbb-222")
    assert err and "blank" in text
    text, err = call(mcp, "diff", old=str(s1), new="   ")
    assert err and "blank" in text


def test_diff_reports_a_mistyped_path_as_a_path(mcp, tmp_path, monkeypatch):
    # A value that looks like a path (separator, or a .jsonl suffix) is never a
    # session id, so the error must say the file is missing rather than send
    # the user hunting for a session called "/nope/typo.jsonl".
    _proj, _s1, _s2 = seed(tmp_path, monkeypatch)
    text, err = call(mcp, "diff", old=str(tmp_path / "nope" / "typo.jsonl"), new="bbb-222")
    assert err and "transcript not found" in text and "typo.jsonl" in text
    text, err = call(mcp, "diff", old="gone.jsonl", new="bbb-222")
    assert err and "transcript not found" in text


def test_history_json_matches_run_history_and_markdown_renders(mcp, tu, tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    data = json.loads(call(mcp, "history", by="command", since="2026-01-01")[0])
    assert data.pop("warnings") == []          # MCP-only key, like transcript
    assert data == tu.run_history(by="command", since="2026-01-01")
    md, err = call(mcp, "history", by="project", format="markdown")
    assert not err and md.startswith("| Project | Calls |")


def test_history_bad_since_is_a_tool_error(mcp, tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    text, err = call(mcp, "history", since="last tuesday")
    assert err and "invalid since value" in text


def test_insights_session_mode_names_transcript(mcp, tmp_path, monkeypatch):
    _proj, _s1, s2 = seed(tmp_path, monkeypatch)
    data = json.loads(call(mcp, "insights", session_id="bbb-222", budget_usd=1.0)[0])
    assert data["mode"] == "session" and data["transcript"] == str(s2)
    assert isinstance(data["findings"], list)
    md, err = call(mcp, "insights", session_id="bbb-222", format="markdown")
    assert not err
    # Whatever the rules find (or don't), the markdown names the session.
    assert f"(session: {s2.parent.name}/{s2.name})" in md
    assert md.startswith(("No notable findings.", "- ["))


def test_insights_window_mode(mcp, tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    data = json.loads(call(mcp, "insights", since="2026-01-01")[0])
    assert data["mode"] == "window" and data["baseline"]["sessions"] == 2
    text, err = call(mcp, "insights", since="7d", session_id="bbb-222")
    assert err and "not both" in text


def test_insights_blank_transcript_is_rejected_even_with_since(mcp, tmp_path, monkeypatch):
    # A present-but-blank selector is a mistake in both modes: it must not be
    # silently ignored into window mode just because `since` is also set.
    seed(tmp_path, monkeypatch)
    text, err = call(mcp, "insights", transcript="", since="7d")
    assert err and "not both" in text
    text, err = call(mcp, "insights", session_id="", since="7d")
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
    proj, _s1, _s2 = seed(tmp_path, monkeypatch)
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
    _proj, _s1, s2 = seed(tmp_path, monkeypatch)
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
    _proj, _s1, s2 = seed(tmp_path, monkeypatch)
    baseline_cost = tu.aggregate(tu.parse_session(s2), tu.load_pricing())["total"]["cost_usd"]
    budget = baseline_cost / 0.8
    monkeypatch.setenv("TOKEN_USAGE_BUDGET_USD", str(budget))
    data = json.loads(call(mcp, "insights", session_id="bbb-222")[0])
    assert any(f["rule"] == "budget-pace" for f in data["findings"])


def test_mcp_json_registers_server_with_plugin_root_paths():
    cfg = json.loads((PLUGIN_ROOT / ".mcp.json").read_text())
    srv = cfg["mcpServers"]["token-usage"]
    assert srv["command"] == "python3"
    assert srv["args"] == ["${CLAUDE_PLUGIN_ROOT}/scripts/mcp_server.py"]
    assert srv["env"] == {"TOKEN_USAGE_PROJECT_DIR": "${CLAUDE_PROJECT_DIR}"}


def test_end_to_end_over_pipes(tmp_path, monkeypatch):
    proj, _s1, s2 = seed(tmp_path, monkeypatch)
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
                       text=True, env=env, timeout=30, check=False)
    assert r.returncode == 0, r.stderr
    assert r.stderr == ""
    replies = {m["id"]: m for m in (json.loads(l) for l in r.stdout.splitlines())}
    assert set(replies) == {1, 2, 3, 4}
    assert replies[1]["result"]["protocolVersion"] == "2025-06-18"
    assert {t["name"] for t in replies[2]["result"]["tools"]} == TOOL_NAMES
    cost = json.loads(replies[3]["result"]["content"][0]["text"])
    assert cost["transcript"] == str(s2)                # TOKEN_USAGE_PROJECT_DIR honoured
    assert replies[4]["result"]["content"][0]["text"].startswith("| Session | Project |")


def test_project_dir_from_env_treats_unexpanded_and_blank_as_unset(mcp, monkeypatch):
    # ${CLAUDE_PROJECT_DIR} only reaches stdio MCP servers from Claude Code
    # 2.1.139; older builds leave the literal placeholder (or an empty string)
    # in the env, and an explicit project dir fails closed — so anything that
    # isn't a real path must read as "no project dir" and fall back to discovery.
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    for raw in ("${CLAUDE_PROJECT_DIR}", "", "   "):
        monkeypatch.setenv("TOKEN_USAGE_PROJECT_DIR", raw)
        assert mcp.project_dir_from_env() is None, raw
    monkeypatch.setenv("TOKEN_USAGE_PROJECT_DIR", " /Users/x/alpha ")
    assert mcp.project_dir_from_env() == "/Users/x/alpha"
    # Claude Code exports CLAUDE_PROJECT_DIR to stdio servers too, so a
    # user-scope registration with no env block still gets a project anchor.
    monkeypatch.delenv("TOKEN_USAGE_PROJECT_DIR")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/Users/x/beta")
    assert mcp.project_dir_from_env() == "/Users/x/beta"
    monkeypatch.setenv("TOKEN_USAGE_PROJECT_DIR", "${CLAUDE_PROJECT_DIR}")
    assert mcp.project_dir_from_env() == "/Users/x/beta"


def test_session_cost_survives_an_unexpanded_or_blank_project_dir(mcp, tmp_path, monkeypatch):
    _proj, _s1, s2 = seed(tmp_path, monkeypatch)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    for raw in ("${CLAUDE_PROJECT_DIR}", ""):
        monkeypatch.setenv("TOKEN_USAGE_PROJECT_DIR", raw)
        data = json.loads(call(mcp, "session_cost")[0])
        assert data["transcript"] == str(s2), raw       # newest anywhere, via discovery
        assert data["resolved_via"] == "any_project"


def test_session_cost_falls_back_to_claude_project_dir(mcp, tmp_path, monkeypatch):
    _proj, s1, _s2 = seed(tmp_path, monkeypatch)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/Users/x/alpha")
    data = json.loads(call(mcp, "session_cost")[0])
    assert data["transcript"] == str(s1) and data["resolved_via"] == "project_dir"


def test_session_cost_reports_the_rung_and_flags_a_guess(mcp, tmp_path, monkeypatch):
    _proj, s1, s2 = seed(tmp_path, monkeypatch)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    assert json.loads(call(mcp, "session_cost", transcript=str(s1))[0])["resolved_via"] == "explicit"
    assert json.loads(call(mcp, "session_cost", session_id="bbb-222")[0])["resolved_via"] == "session_id"
    # A guessed session (no project dir) says so in markdown as well as JSON.
    md, err = call(mcp, "session_cost", format="markdown")
    assert not err
    assert "no project dir was supplied" in md and s2.parent.name in md
    # A session the caller named is not a guess, so no note.
    md, err = call(mcp, "session_cost", transcript=str(s1), format="markdown")
    assert "no project dir was supplied" not in md


def test_insights_reports_the_rung_and_flags_a_guess(mcp, tmp_path, monkeypatch):
    _proj, _s1, _s2 = seed(tmp_path, monkeypatch)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    data = json.loads(call(mcp, "insights")[0])
    assert data["resolved_via"] == "any_project"
    md, err = call(mcp, "insights", format="markdown")
    assert not err and "no project dir was supplied" in md


def test_broken_env_transcript_is_diagnosed(mcp, tmp_path, monkeypatch):
    _proj, _s1, _s2 = seed(tmp_path, monkeypatch)
    gone = tmp_path / "gone.jsonl"
    monkeypatch.setenv("TOKEN_USAGE_TRANSCRIPT", str(gone))
    text, err = call(mcp, "session_cost")
    assert err and text == (f"TOKEN_USAGE_TRANSCRIPT is set to {gone} "
                            "but that file does not exist")


def test_nothing_found_names_projects_dir_project_dir_and_session_id(mcp, tmp_path, monkeypatch):
    proj, _s1, _s2 = seed(tmp_path, monkeypatch)
    monkeypatch.setenv("TOKEN_USAGE_PROJECT_DIR", "/Users/x/no-sessions-yet")
    text, err = call(mcp, "session_cost")
    assert err
    assert str(proj) in text and "/Users/x/no-sessions-yet" in text and "session_id" in text


def write_overlay(monkeypatch, tmp_path, text):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    overlay = TOKEN_USAGE.user_pricing_path()
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(text)
    return overlay


def test_pricing_overlay_problems_reach_the_caller(mcp, tmp_path, monkeypatch):
    # A malformed overlay reverts costs to bundled rates. On the CLI that is a
    # stderr line; over MCP stderr is invisible, so it has to be in the result.
    _proj, s1, _s2 = seed(tmp_path, monkeypatch)
    overlay = write_overlay(monkeypatch, tmp_path, "{not json")
    data = json.loads(call(mcp, "session_cost", transcript=str(s1))[0])
    assert data["warnings"] == [f"ignoring malformed pricing file {overlay}"]
    md, err = call(mcp, "session_cost", transcript=str(s1), format="markdown")
    assert not err and f"Warning: ignoring malformed pricing file {overlay}" in md


def test_every_tool_carries_a_warnings_key(mcp, tmp_path, monkeypatch):
    _proj, s1, _s2 = seed(tmp_path, monkeypatch)
    overlay = write_overlay(monkeypatch, tmp_path, "{not json")
    expected = [f"ignoring malformed pricing file {overlay}"]
    for name, args in (("session_cost", {"transcript": str(s1)}),
                       ("history", {"since": "2026-01-01"}),
                       ("insights", {"session_id": "bbb-222"}),
                       ("insights", {"since": "2026-01-01"}),
                       ("diff", {"old": "aaa-111", "new": "bbb-222"}),
                       ("top_consumers", {"since": "2026-01-01"})):
        text, err = call(mcp, name, **args)
        assert not err, (name, text)
        assert json.loads(text)["warnings"] == expected, name


def test_clean_overlay_means_no_warnings(mcp, tmp_path, monkeypatch):
    _proj, s1, _s2 = seed(tmp_path, monkeypatch)
    write_overlay(monkeypatch, tmp_path, json.dumps({"claude-x": {"input": 1.0, "output": 2.0}}))
    assert json.loads(call(mcp, "session_cost", transcript=str(s1))[0])["warnings"] == []
    md, _ = call(mcp, "session_cost", transcript=str(s1), format="markdown")
    assert "Warning:" not in md


def test_junk_budget_env_is_reported_as_a_warning(mcp, tmp_path, monkeypatch):
    _proj, _s1, _s2 = seed(tmp_path, monkeypatch)
    monkeypatch.setenv("TOKEN_USAGE_BUDGET_USD", "ten pounds")
    data = json.loads(call(mcp, "insights", session_id="bbb-222")[0])
    assert data["warnings"] == ["ignoring TOKEN_USAGE_BUDGET_USD='ten pounds' — not a number"]


def test_budget_usd_must_be_positive(mcp, tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    assert mcp.SCHEMAS["insights"]["properties"]["budget_usd"]["exclusiveMinimum"] == 0
    text, err = call(mcp, "insights", session_id="bbb-222", budget_usd=0)
    assert err and "budget_usd must be > 0" in text
    schema = {"type": "object", "additionalProperties": False,
              "properties": {"n": {"type": "number", "exclusiveMinimum": 0}}}
    assert mcp.validate_args(schema, {"n": 0.0}) == ["n must be > 0"]
    assert mcp.validate_args(schema, {"n": 0.1}) == []


def test_exception_path_prints_a_traceback_and_restores_stdout(mcp, capsys):
    real = sys.stdout
    mcp.SCHEMAS["boom2"] = {"type": "object", "properties": {}, "additionalProperties": False}
    mcp.HANDLERS["boom2"] = lambda args: 1 / 0
    r = mcp.call_tool("boom2", {})
    assert sys.stdout is real
    out, err = capsys.readouterr()
    assert out == ""                                  # stdout stays JSON-RPC only
    assert r["isError"] and "ZeroDivisionError" in r["content"][0]["text"]
    assert "Traceback (most recent call last)" in err and "ZeroDivisionError" in err


def test_bare_sys_exit_status_is_reported_not_swallowed(mcp, capsys):
    # sys.exit(1) sets code to an int, whose str() is a bare "1" — useless as a
    # tool error message. Only a non-empty string code is a message.
    real = sys.stdout
    mcp.SCHEMAS["quitter"] = {"type": "object", "properties": {}, "additionalProperties": False}

    def quitter(args):
        sys.exit(1)
    mcp.HANDLERS["quitter"] = quitter
    r = mcp.call_tool("quitter", {})
    assert sys.stdout is real
    assert r["isError"] and r["content"][0]["text"] == "quitter: exited with status 1"

    def silent(args):
        sys.exit()
    mcp.HANDLERS["silent"] = silent
    mcp.SCHEMAS["silent"] = mcp.SCHEMAS["quitter"]
    r = mcp.call_tool("silent", {})
    assert sys.stdout is real
    assert r["isError"] and r["content"][0]["text"] == "silent: exited with status None"
    capsys.readouterr()


def test_since_errors_use_the_tools_own_vocabulary(mcp, tmp_path, monkeypatch):
    # The MCP caller never typed "--since", so the analyser's CLI flag name is
    # noise in a tool error.
    seed(tmp_path, monkeypatch)
    for name in ("history", "insights", "top_consumers"):
        text, err = call(mcp, name, since="last tuesday")
        assert err and "invalid since value" in text, name
        assert "--since" not in text, name


def test_transcript_and_session_id_together_are_rejected(mcp, tmp_path, monkeypatch):
    _proj, s1, _s2 = seed(tmp_path, monkeypatch)
    for name in ("session_cost", "insights"):
        text, err = call(mcp, name, transcript=str(s1), session_id="bbb-222")
        assert err and text == "pass transcript OR session_id, not both", name


def test_insights_project_is_window_mode_only(mcp, tmp_path, monkeypatch):
    _proj, _s1, _s2 = seed(tmp_path, monkeypatch)
    text, err = call(mcp, "insights", session_id="bbb-222", project="beta")
    assert err and text == "project applies to window mode (with since)"
    # project ALONE is the dangerous case: with no session selector it used to
    # fall into session mode with the filter dropped, answering about whatever
    # session discovery found -- in another project entirely.
    text, err = call(mcp, "insights", project="alpha")
    assert err and text == "project applies to window mode (with since)"
    text, err = call(mcp, "insights", project="alpha", since="2026-01-01")
    assert not err                                   # window mode is the way to filter
    assert mcp.SCHEMAS["insights"]["properties"]["project"]["description"].endswith(
        "Window mode only: pass it with since.")
    # The shared PROJECT description is untouched for the tools that filter.
    assert mcp.SCHEMAS["history"]["properties"]["project"] == mcp.PROJECT


def test_blank_window_selectors_are_rejected(mcp, tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    assert mcp.SINCE["minLength"] == 1 and mcp.PROJECT["minLength"] == 1
    text, err = call(mcp, "history", since="")
    assert err and "since must be at least 1 character" in text
    text, err = call(mcp, "history", project="")
    assert err and "project must be at least 1 character" in text
    schema = {"type": "object", "additionalProperties": False,
              "properties": {"s": {"type": "string", "minLength": 1}}}
    assert mcp.validate_args(schema, {"s": "x"}) == []


def test_plugin_version_survives_a_broken_manifest(mcp, tmp_path, monkeypatch, capsys):
    bad = tmp_path / "plugin.json"
    bad.write_text("[]")                       # valid JSON, wrong shape -> AttributeError
    monkeypatch.setattr(mcp, "plugin_manifest_path", lambda: bad)
    assert mcp.plugin_version() == "0"
    err = capsys.readouterr().err
    assert str(bad) in err and err.count("plugin manifest") == 1
    monkeypatch.setattr(mcp, "plugin_manifest_path", lambda: tmp_path / "gone.json")
    assert mcp.plugin_version() == "0"
    assert "plugin manifest" in capsys.readouterr().err


def test_batch_arrays_are_rejected_and_documented(mcp):
    assert "batch" in mcp.handle_message.__doc__
    r = mcp.handle_message([{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
    assert r["error"]["code"] == -32600


def test_handle_message_edges(mcp):
    # params must be an object: a list is a protocol-level mistake, not a
    # tool error.
    r = mcp.handle_message({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                            "params": [1, 2]})
    assert r["error"]["code"] == -32602 and r["id"] == 8
    # A non-string method is an invalid request, not "method not found".
    r = mcp.handle_message({"jsonrpc": "2.0", "id": 9, "method": 42})
    assert r["error"]["code"] == -32600 and r["id"] == 9
    # An explicit null id is a request (the key is present), so it is answered
    # — with id null, as JSON-RPC requires.
    r = mcp.handle_message({"jsonrpc": "2.0", "id": None, "method": "ping"})
    assert r == {"jsonrpc": "2.0", "id": None, "result": {}}


def test_validate_args_rejects_a_float_for_an_integer(mcp):
    schema = mcp.SCHEMAS["top_consumers"]
    assert mcp.validate_args(schema, {"limit": 2.0}) == ["limit must be an integer"]
    assert mcp.validate_args(schema, {"limit": 2}) == []


def test_looks_like_path_recognises_a_bare_relative_path(mcp):
    assert mcp._looks_like_path("a/b") is True
    assert mcp._looks_like_path("sess.jsonl") is True
    assert mcp._looks_like_path("aaa-111") is False


def test_session_cost_agents_flag_is_passed_through(mcp, tmp_path, monkeypatch):
    proj, _s1, _s2 = seed(tmp_path, monkeypatch)
    t = write_jsonl(proj / "-Users-x-alpha" / "agy-777.jsonl", [
        user("2026-06-15T10:00:00Z", command="/code-review"),
        assistant("2026-06-15T10:00:01Z", usage(out=100), request_id="m1"),
    ])
    sub = t.parent / t.stem / "subagents"
    write_jsonl(sub / "agent-001.jsonl",
                [assistant("2026-06-15T10:00:30Z", usage(out=40), request_id="a1")])
    (sub / "agent-001.meta.json").write_text('{"agentType": "Explore", "description": "scan"}')
    md, err = call(mcp, "session_cost", transcript=str(t), format="markdown", agents=True)
    assert not err and "↳ Explore" in md
    plain, err = call(mcp, "session_cost", transcript=str(t), format="markdown")
    assert not err and "↳ Explore" not in plain


def test_server_survives_undecodable_stdin_bytes(tmp_path, monkeypatch):
    # The decode happens in the TextIOWrapper, outside every handler: one 0xff
    # byte used to kill the process with rc=1 and ZERO replies — losing the
    # already-valid request queued ahead of it.
    proj, _s1, _s2 = seed(tmp_path, monkeypatch)
    env = dict(os.environ, TOKEN_USAGE_PROJECTS_DIR=str(proj),
               TOKEN_USAGE_LEDGER_DIR=str(tmp_path / "cache"),
               PYTHONIOENCODING="utf-8")   # strict UTF-8: the desktop locale
    script = (json.dumps(req("ping", id_=1)).encode() + b"\n"
              + b"\xff\xfe{\"jsonrpc\": \"2.0\"}\n"
              + json.dumps(req("ping", id_=2)).encode() + b"\n")
    r = subprocess.run([sys.executable, str(SERVER)], input=script, capture_output=True,
                       env=env, timeout=30, check=False)
    assert r.returncode == 0, r.stderr
    replies = [json.loads(line) for line in r.stdout.decode().splitlines()]
    assert [m.get("id") for m in replies if "result" in m] == [1, 2]
    assert [m["error"]["code"] for m in replies if "error" in m] == [-32700]
