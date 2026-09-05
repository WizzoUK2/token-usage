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
