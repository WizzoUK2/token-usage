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
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import token_usage as tu

SUPPORTED_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
LATEST_VERSION = "2025-06-18"

FORMAT = {"type": "string", "enum": ["json", "markdown"],
          "description": "json (default): structured data — the CLI's json shapes plus "
                         "transcript, resolved_via and warnings. "
                         "markdown: the rendered table/text."}
SESSION_SELECTORS = {
    "transcript": {"type": "string", "description": "Path to a session .jsonl transcript."},
    "session_id": {"type": "string",
                   "description": "Claude Code session id; searched across every project."},
}
SINCE = {"type": "string", "minLength": 1,
         "description": "Window start: Nd (e.g. 7d) or YYYY-MM-DD."}
PROJECT = {"type": "string", "minLength": 1,
           "description": "Substring filter on the project slug."}
INSIGHTS_PROJECT = dict(PROJECT, description=PROJECT["description"]
                        + " Window mode only: pass it with since.")


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
                    "Defaults to the newest session for TOKEN_USAGE_PROJECT_DIR (Claude Code) "
                    "or, without one, the newest session found on the machine. Result names "
                    "the transcript analysed.",
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
     "inputSchema": _schema(dict(SESSION_SELECTORS, since=SINCE, project=INSIGHTS_PROJECT,
                                 budget_usd={"type": "number", "exclusiveMinimum": 0,
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
                             "since": dict(SINCE,
                                           description=SINCE["description"] + " (default 30d)"),
                             "project": PROJECT,
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
            article = "an" if typ[0] in "aeiou" else "a"
            problems.append(f"{key} must be {article} {typ}")
            continue
        if "enum" in spec and val not in spec["enum"]:
            problems.append(f"{key} must be one of {spec['enum']}")
        if "minimum" in spec and val < spec["minimum"]:
            problems.append(f"{key} must be >= {spec['minimum']}")
        if "exclusiveMinimum" in spec and val <= spec["exclusiveMinimum"]:
            problems.append(f"{key} must be > {spec['exclusiveMinimum']}")
        if "minLength" in spec and len(val) < spec["minLength"]:
            problems.append(f"{key} must be at least {spec['minLength']} character(s)")
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
        # sys.exit(1) leaves an int (or None) behind, whose str() is no
        # explanation at all — only a non-empty string code is a message.
        message = e.code if isinstance(e.code, str) and e.code else None
        return _tool_result(message or f"{name}: exited with status {e.code!r}", True)
    except Exception as e:  # noqa: BLE001 — a tool must not take the server down
        # The isError text is one line; the traceback behind it goes to stderr,
        # where the host logs it. The happy path still writes nothing there.
        traceback.print_exc(file=sys.stderr)
        return _tool_result(f"{type(e).__name__}: {e}", True)
    finally:
        sys.stdout = real_stdout


def plugin_manifest_path():
    return Path(__file__).resolve().parent.parent / ".claude-plugin" / "plugin.json"


def plugin_version():
    """Version reported in initialize, or "0" — a manifest problem must not
    stop the handshake, but it must not pass unmentioned either."""
    manifest = plugin_manifest_path()
    try:
        return str(json.loads(manifest.read_text()).get("version", "0"))
    except (OSError, ValueError, AttributeError) as e:  # missing, malformed, or not an object
        print(f"token-usage: cannot read plugin manifest {manifest}: {e}", file=sys.stderr)
        return "0"


def _result(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def handle_message(msg):
    """Route one JSON-RPC message. Returns a response dict, or None for notifications.

    One object per message: a JSON-RPC batch (an array of messages) is not
    supported and comes back as -32600 invalid request, which the 2025-06-18
    MCP revision removed anyway."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "invalid request")
    if "id" not in msg:
        return None  # notification (initialized, cancelled, ...) — JSON-RPC forbids a reply
    method, id_ = msg["method"], msg["id"]
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
            try:
                reply = handle_message(msg)
            except Exception as e:  # noqa: BLE001 — one bad message must not end the session
                reply = _error(msg.get("id") if isinstance(msg, dict) else None,
                               -32603, f"internal error: {type(e).__name__}: {e}")
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()


# --- tool handlers -----------------------------------------------------------

def project_dir_from_env():
    """The project dir to anchor default session resolution on, or None.

    TOKEN_USAGE_PROJECT_DIR first (the plugin's .mcp.json sets it from
    ${CLAUDE_PROJECT_DIR}), then CLAUDE_PROJECT_DIR itself, which Claude Code
    exports to stdio MCP servers from 2.1.139 — so a user-scope registration
    with no env block is anchored too. A value that is blank or still carries
    an unexpanded "${...}" placeholder (older hosts, other clients) counts as
    unset: an explicit project dir fails closed, so a bogus one would turn
    every default call into an error instead of falling back to discovery."""
    for name in ("TOKEN_USAGE_PROJECT_DIR", "CLAUDE_PROJECT_DIR"):
        value = (os.environ.get(name) or "").strip()
        if value and not value.startswith("${"):
            return value
    return None


# Rungs that mean "nobody named this session; discovery guessed it".
GUESSED_RUNGS = ("cwd", "any_project")


def check_since(value, key="since"):
    """Validate a window value the way the analyser will, but complain in the
    tool's own vocabulary — an MCP caller never typed a "--since" flag."""
    if value is None:
        return
    try:
        tu.since_cutoff(value, flag=key)
    except SystemExit as e:
        raise ToolError(str(e.code).replace("token-usage: ", "", 1)) from None


def pick_transcript(path=None, session_id=None):
    """(transcript, rung) for a session selector, or ToolError saying what was
    tried. Resolution order: see token_usage.locate_transcript_with_source
    (authoritative)."""
    if path is not None and not path.strip():
        raise ToolError("transcript must not be blank")
    if session_id is not None and not session_id.strip():
        raise ToolError("session_id must not be blank")
    if path and session_id:
        raise ToolError("pass transcript OR session_id, not both")
    project_dir = project_dir_from_env()
    t, via = tu.locate_transcript_with_source(path, session_id=session_id,
                                              project_dir=project_dir)
    if t:
        return t, via
    if via == "explicit":
        raise ToolError(f"transcript not found: {path}")
    if via == "session_id":
        raise ToolError(f"no transcript for session id {session_id!r} under {tu.projects_dir()}")
    if via == "env":
        raise ToolError("TOKEN_USAGE_TRANSCRIPT is set to "
                        f"{os.environ.get('TOKEN_USAGE_TRANSCRIPT')} "
                        "but that file does not exist")
    where = f"{tu.projects_dir()}" + (f" (project dir {project_dir})" if project_dir else "")
    raise ToolError(f"no transcript found under {where}; pass transcript or session_id")


def guess_note(transcript, via):
    """Markdown footnotes for a session nobody actually named."""
    if via not in GUESSED_RUNGS:
        return []
    return [("Note: no project dir was supplied; this is the newest transcript under "
             f"{transcript.parent.name}, which may not be the session you meant.")]


def finish(data, render, fmt, warnings, footnotes=()):
    """JSON payload (always carrying "warnings"), or the rendered markdown with
    each footnote — the warnings included — as its own block."""
    data["warnings"] = warnings
    if fmt != "markdown":
        return json.dumps(data)
    return "\n\n".join([render(data), *footnotes, *(f"Warning: {w}" for w in warnings)])


def tool_session_cost(args):
    warnings = []
    t, via = pick_transcript(args.get("transcript"), args.get("session_id"))
    data = tu.aggregate(tu.parse_session(t), tu.load_pricing(warnings))
    data["transcript"] = data["transcript_path"] = str(t)
    data["resolved_via"] = via
    return finish(data,
                  lambda d: tu.render_report(d, show_agents=bool(args.get("agents")),
                                             show_models=bool(args.get("models"))),
                  args.get("format"), warnings, guess_note(t, via))


def _looks_like_path(value):
    """A session id is a bare uuid-ish token, so anything carrying a path
    separator or a .jsonl suffix was meant as a path — including a mistyped
    one, which must be reported as a missing file rather than as an unknown id."""
    seps = [os.sep] + ([os.altsep] if os.altsep else [])
    return value.endswith(".jsonl") or any(sep in value for sep in seps)


def _path_or_id(value):
    """diff accepts either form per side: a path-shaped value is a path, else a session id."""
    if not value.strip():
        raise ToolError("old/new must not be blank")
    return (pick_transcript(path=value) if _looks_like_path(value)
            else pick_transcript(session_id=value))[0]


def tool_diff(args):
    warnings = []
    old, new = _path_or_id(args["old"]), _path_or_id(args["new"])
    data = tu.diff_data(old, new, tu.load_pricing(warnings))
    return finish(data, tu.render_diff, args.get("format"), warnings)


def tool_history(args):
    warnings = []
    check_since(args.get("since"))
    data = tu.run_history(by=args.get("by", "project"), since=args.get("since"),
                          project=args.get("project"), warnings=warnings)
    return finish(data, tu.render_history, args.get("format"), warnings)


def tool_insights(args):
    # Presence, not truthiness: a blank transcript is a mistake to report, not
    # a value to ignore into window mode.
    has_session = args.get("transcript") is not None or args.get("session_id") is not None
    if has_session and args.get("since"):
        raise ToolError("pass a transcript/session_id OR since, not both")
    # project filters the window scan and nothing else: session mode has no
    # use for it, so accepting it there would answer about a session picked by
    # discovery -- quite possibly in another project.
    if args.get("project") is not None and not args.get("since"):
        raise ToolError("project applies to window mode (with since)")
    check_since(args.get("since"))
    warnings = []
    budget = args.get("budget_usd")
    if budget is None:
        budget = tu.budget_from_env(warnings)
    footnotes = []
    if args.get("since"):
        data = tu.run_insights(since=args["since"], project=args.get("project"),
                               budget=budget, warnings=warnings)
    else:
        t, via = pick_transcript(args.get("transcript"), args.get("session_id"))
        data = tu.run_insights(transcript=str(t), budget=budget, warnings=warnings)
        data["transcript"] = str(t)
        data["resolved_via"] = via
        footnotes = guess_note(t, via)
    return finish(data, tu.render_insights, args.get("format"), warnings, footnotes)


def tool_top_consumers(args):
    warnings = []
    check_since(args.get("since", "30d"))
    data = tu.run_top_consumers(by=args.get("by", "session"), since=args.get("since", "30d"),
                                project=args.get("project"), limit=args.get("limit", 10),
                                warnings=warnings)
    return finish(data, tu.render_top_consumers, args.get("format"), warnings)


HANDLERS.update({"session_cost": tool_session_cost, "diff": tool_diff,
                 "history": tool_history, "insights": tool_insights,
                 "top_consumers": tool_top_consumers})


if __name__ == "__main__":
    try:
        serve()
    except (KeyboardInterrupt, BrokenPipeError):
        pass
