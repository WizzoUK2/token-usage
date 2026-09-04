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
