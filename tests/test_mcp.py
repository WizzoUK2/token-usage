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
