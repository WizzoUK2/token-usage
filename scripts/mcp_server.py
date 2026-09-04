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
    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str) or not name:
            return _error(id_, -32602, "tools/call requires params.name")
        return _result(id_, call_tool(name, params.get("arguments") or {}))
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
