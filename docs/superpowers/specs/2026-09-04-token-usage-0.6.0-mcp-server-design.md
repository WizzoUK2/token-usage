# token-usage 0.6.0 — MCP server design

**Date:** 2026-09-04
**Status:** approved in brainstorming; implementation plan to follow

## Context and roadmap position

0.5.0 finished the interpretation arc (insights, pricing overlay, per-day
history). The 0.5.0 spec agreed the next release as:

> **0.6.0 — MCP server:** bundled stdlib stdio MCP server exposing cost
> queries as tools, registered via the plugin's `.mcp.json`. Primary
> motivation: Cowork has no hooks/ledger; MCP gives it structured access.

This spec designs that release. Consumers, in priority order:

1. **Claude Code** — the plugin's own `.mcp.json` auto-starts the server
   when the plugin is enabled; tools give structured access without the
   skill shelling out to the CLI.
2. **Cowork / Claude desktop** — registered as a local stdio server in the
   desktop app config; runs on the host and reads `~/.claude/projects`
   directly, outside any sandbox.

Fleet / multi-machine aggregation remains explicitly not a driver.

## Hard constraints (unchanged)

- Python 3.9+ stdlib only. Zero dependencies. No install step.
- No LLM calls, no network, no telemetry.
- Nothing the server does can affect the hook path or the ledger.
- The analyser stays in `scripts/token_usage.py`; the server is a second
  stdlib file that imports it. ("Single script" in the 0.5.0 spec meant no
  package structure or install step; a sibling file satisfies that.)

## Decisions

| Question | Decision |
|---|---|
| Implementation | Hand-rolled JSON-RPC 2.0 over stdio in `scripts/mcp_server.py`; no `mcp` SDK. |
| Tools | `session_cost`, `history`, `insights`, `diff`, `top_consumers`. |
| Result format | JSON by default (same shapes as the CLI's `json`/`--json` output); `format: "markdown"` returns the rendered CLI text. |
| "Current session" | Resolved from `TOKEN_USAGE_PROJECT_DIR` (set from `${CLAUDE_PROJECT_DIR}` in `.mcp.json`); falls back to the newest transcript anywhere. The result always names the transcript analysed. |
| Caching | None in the server process. Pricing reloads per call; history uses the shared on-disk index. |

## 1. Architecture

```
Claude Code / Claude desktop
        │  stdio, newline-delimited JSON-RPC 2.0
        ▼
scripts/mcp_server.py        ← protocol loop, tool schemas, arg validation, dispatch
        │  plain function calls
        ▼
scripts/token_usage.py       ← parse / aggregate / history / insights / diff / top_consumers
        │
        ▼
~/.claude/projects/**.jsonl  +  ~/.cache/token-usage/index/  +  pricing (bundled + overlay)
```

`mcp_server.py` adds its own directory to `sys.path` and imports
`token_usage` as a module. It contains no cost logic.

### Protocol subset

| Method | Behaviour |
|---|---|
| `initialize` | Returns `protocolVersion`, `capabilities: {"tools": {}}`, `serverInfo: {name: "token-usage", version: <plugin version>}`. Echoes the client's requested version if it is one of `2024-11-05`, `2025-03-26`, `2025-06-18`; otherwise answers with `2025-06-18`. |
| `notifications/initialized` | Accepted, no response (notification). |
| `ping` | `{}`. |
| `tools/list` | The five tool definitions with JSON Schema `inputSchema`. |
| `tools/call` | Validates arguments, dispatches, returns `{"content": [{"type": "text", "text": ...}], "isError": bool}`. |
| anything else | JSON-RPC error `-32601` (method not found). Notifications for unknown methods are ignored. |

Framing: one JSON object per line on stdin and stdout, UTF-8. Logging goes
to stderr only; nothing but JSON-RPC ever reaches stdout. EOF on stdin
exits 0.

### Registration

`.mcp.json` at the plugin root:

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

Tools surface in Claude Code as `mcp__plugin_token-usage_token-usage__<tool>`.

For Claude desktop / Cowork the README documents the equivalent entry for
`claude_desktop_config.json` (absolute path to the script, no env), and the
`claude mcp add token-usage -- python3 <path>/scripts/mcp_server.py` form.

## 2. Tools

All tools accept `format`: `"json"` (default) or `"markdown"`. JSON results
are the CLI's existing structures; markdown results are the CLI's rendered
text. Unknown argument keys are rejected.

### `session_cost`

Per-activity breakdown of one session.

| Arg | Type | Notes |
|---|---|---|
| `transcript` | string | Path to a session `.jsonl`. |
| `session_id` | string | Searched as `<projects>/*/<id>.jsonl`. |
| `agents` | boolean | Per-agent-type ↳ rows (markdown) / always present in JSON. |
| `models` | boolean | Per-model ↳ rows (markdown) / always present in JSON. |
| `format` | enum | |

JSON: the `aggregate()` output plus `"transcript": "<path analysed>"`.

### `history`

| Arg | Type | Notes |
|---|---|---|
| `by` | enum `project`, `day`, `command`, `model` | default `project` |
| `since` | string | `Nd` or `YYYY-MM-DD`, as the CLI |
| `project` | string | substring filter |
| `format` | enum | |

JSON: `run_history()` output. Markdown: `render_history()`.

### `insights`

| Arg | Type | Notes |
|---|---|---|
| `transcript`, `session_id` | string | session mode (default: current session) |
| `since`, `project` | string | window mode; `since` present ⇒ window mode |
| `budget_usd` | number | overrides `TOKEN_USAGE_BUDGET_USD` for this call |
| `format` | enum | |

JSON: `run_insights()` output plus `"transcript"` in session mode.
Markdown: `render_insights()`.

### `diff`

| Arg | Type | Notes |
|---|---|---|
| `old`, `new` | string, required | each a transcript path or session id |
| `format` | enum | |

JSON: `diff_data()` output. Markdown: `render_diff()`.

### `top_consumers`

The one capability the CLI lacks today; it gains a matching CLI subcommand
(`top_consumers --by session|command [--since] [--project] [--limit] [--json]`)
for parity.

| Arg | Type | Notes |
|---|---|---|
| `by` | enum `session`, `command` | default `session` |
| `since` | string | default `30d` |
| `project` | string | substring filter |
| `limit` | integer ≥ 1 | default 10 |
| `format` | enum | |

Implementation in the analyser: iterate `cached_summary()` over the
projects dir (same loop and filters as `run_history`).

- `by: session` — one row per transcript: `path`, `session_id` (file
  stem), `project`, `first_ts`, `usage`, `cost_usd`. Sorted by cost desc
  (unpriced → treated as 0 and listed last), truncated to `limit`.
- `by: command` — one row per label aggregated across sessions: `label`,
  `sessions` (count of transcripts containing it), `invocations`, `usage`,
  `cost_usd`. Same sort and truncation.

JSON: `{"by", "since", "project", "limit", "rows": [...], "unpriced_models": [...]}`.
Markdown: a table with the same columns as `history` plus a leading
Session/Command column; unpriced footnote as elsewhere.

### Session resolution

Used by `session_cost`, `insights` (session mode) and `diff`. Order:

1. explicit `transcript` path — must exist, else error;
2. `session_id` — glob `<projects>/*/<id>.jsonl`; zero matches error,
   several matches pick the newest by mtime;
3. `TOKEN_USAGE_TRANSCRIPT` env var — must exist, else error (it overrides
   an explicitly supplied `project_dir`, same as it overrides cwd);
4. `TOKEN_USAGE_PROJECT_DIR` / caller-supplied `project_dir` set — newest
   `.jsonl` under `<projects>/<slug(project_dir)>/`, or error if that
   project has none. An explicit `project_dir` never falls through to step
   5 — reporting a *different* project's session with no indication would
   be worse than a clean "not found";
5. no `project_dir` at all (no project context to anchor on — Claude
   desktop, or a bare CLI invocation): the cwd's own project directory
   (`<projects>/<slug(cwd)>/`), then the Cowork mount roots (they hold
   exactly the live session), then the newest `.jsonl` under any project.

`diff` takes a path or a session id per side, so it only ever uses steps 1–2;
there is no "current session" default to fall through to.

The analyser gains `locate_transcript(arg=None, session_id=None,
project_dir=None)` returning a `Path` or `None`; the CLI's
`resolve_transcript()` becomes a thin wrapper that exits when it is `None`.
`find_latest_transcript()` takes an optional `project_dir` and uses
`projects_dir()` (so `TOKEN_USAGE_PROJECTS_DIR` works there too).

## 3. Data flow

`tools/call` → `dispatch(name, args)`:

1. look up the tool; unknown name → tool error;
2. validate `args` against the tool's schema (types, enums, required,
   integer minimum, no unknown keys) → tool error listing every problem;
3. resolve transcript(s) where applicable;
4. `pricing = load_pricing()`;
5. call the analyser; render if `format == "markdown"`;
6. return one text content block (`json.dumps(..., indent=None)` or the
   rendered string).

Nothing is cached in-process. Cost per call equals the CLI's: a fresh parse
for session tools, an index-backed scan for window tools.

## 4. Error handling

- **Tool failures are results, not protocol errors** (`isError: true`, one
  text block). Cases: transcript not found (message lists the paths
  searched); unknown session id; invalid arguments; bad `since`; any
  unexpected exception, caught at dispatch and reported as
  `<ExceptionClass>: <message>`.
- **Protocol errors** use JSON-RPC error objects: `-32700` parse error
  (null id) for an unparseable line, `-32600` for a line that is not a
  request object, `-32601` unknown method, `-32602` for `tools/call`
  missing `name`.
- The read loop never dies on bad input; it continues to the next line.
- stdout is reserved for JSON-RPC. The server wraps dispatch so an
  accidental `print` in the analyser cannot corrupt the stream (stdout is
  swapped for stderr during analyser calls).
- Exit 0 on EOF; exit 0 on SIGINT/BrokenPipe.

## 5. Testing

`tests/test_mcp.py`, pytest, existing conftest fixtures.

Unit (pure `handle_message(dict) -> dict | None`, no subprocess):

- `initialize` handshake, version echo and fallback, `serverInfo.version`
  matches `.claude-plugin/plugin.json`;
- `notifications/initialized` and unknown notifications return `None`;
- `ping`;
- `tools/list` names and that every `inputSchema` is valid draft-07 shape
  with `additionalProperties: false`;
- each tool: JSON and markdown format against fixture transcripts;
  `session_cost` JSON carries `transcript`; `history --by model` parity
  with the CLI; `insights` session and window modes; `diff` by path and by
  session id; `top_consumers` by session and by command, `limit`, sort
  order, unpriced last;
- session resolution order, including `session_id` glob across projects
  and `TOKEN_USAGE_PROJECT_DIR`;
- every error path in §4 (argument validation lists all problems, missing
  transcript, unknown method, parse error with null id, unknown tool);
- `print()` inside the analyser does not reach stdout during a call.

Analyser: `top_consumers` logic and `locate_transcript()` get their own
tests in the existing suites.

Integration: one subprocess test spawns `scripts/mcp_server.py`, sends
`initialize`, `notifications/initialized`, `tools/list`, one `tools/call`
over pipes, asserts the responses and that stderr is empty.

CI matrix unchanged (3.9, 3.12); no `match`, no `X | Y` annotations.

## 6. Docs and release

- README: new "MCP server" section — Claude Code auto-registration, tool
  list with one-line descriptions, Claude desktop / `claude mcp add`
  snippets, "current session" resolution rules, and the `top_consumers`
  CLI subcommand.
- `skills/report/SKILL.md`: when the `token-usage` MCP tools are available
  in the session, prefer them over shelling out; the CLI path stays as the
  fallback (Cowork sandbox without the server registered).
- CHANGELOG `[0.6.0]`; `plugin.json` version `0.6.0`; SKILL.md version.

## Out of scope (deferred)

- Dashboard / `--live` mode (0.7.0).
- HTTP/SSE transport; remote or fleet registration.
- Resources or prompts in the MCP surface; tools only.
- Any in-process caching or file watching.
- User-configurable insight thresholds via tool arguments.
