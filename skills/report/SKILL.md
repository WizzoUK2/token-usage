---
name: report
description: Generate a per-activity breakdown of Claude Code or Cowork token usage and estimated API cost for the current or a past session, attributing usage to slash commands (Claude Code) or skills (Cowork), including subagent rollups, per-agent-type and per-model breakdowns, cross-session history with burn rate, compare mode, budget nudge status, and rule-based spend insights. This skill should be used when the user asks "where did my tokens go", "token usage report", "how many tokens did that command or skill use", "what did this session cost", "which command/skill/subagent/model used the most tokens", "show me token history", "what did I spend this week", "what's my burn rate", "token history by day/project/command/model", "compare token usage between two sessions", "which sessions cost the most", "costliest sessions", "top consumers", "any tips on my token spend", "analyse my token usage", or "why was this session expensive".
argument-hint: "[transcript-path]"
allowed-tools: Bash, Read, mcp__plugin_token-usage_token-usage__session_cost, mcp__plugin_token-usage_token-usage__history, mcp__plugin_token-usage_token-usage__insights, mcp__plugin_token-usage_token-usage__diff, mcp__plugin_token-usage_token-usage__top_consumers, mcp__token-usage__session_cost, mcp__token-usage__history, mcp__token-usage__insights, mcp__token-usage__diff, mcp__token-usage__top_consumers
version: 0.6.0
---

# token-usage report

Produce a per-activity token-usage breakdown for the current Claude Code session: which slash commands consumed tokens, how much ad-hoc (non-command) work consumed, subagent rollups, and an estimated API-equivalent cost. Also handles cross-session history and transcript comparison.

## Prefer the MCP tools when present

If tools named `mcp__plugin_token-usage_token-usage__session_cost`, `…__history`,
`…__insights`, `…__diff` or `…__top_consumers` are available in this session, call them
instead of shelling out. (Registered by hand with `claude mcp add --scope user` the same
tools are named `mcp__token-usage__<tool>`; both prefixes are allowed here.) Same data,
structured result, no path resolution needed. Use `format: "markdown"` when the user
wants the table shown verbatim. Fall back to the CLI
below when the tools are absent (e.g. a Cowork sandbox without the server registered).

## How to run

The parser script lives at `../../scripts/token_usage.py` relative to this skill's base directory (i.e. `<plugin-root>/scripts/token_usage.py`). Resolve the plugin root from the "Base directory for this skill" path shown above, then run:

```bash
# Current session — markdown table
python3 "<plugin-root>/scripts/token_usage.py" report [transcript-path]

# Add per-agent-type ↳ breakdown rows (subsets of parent row, not additive)
python3 "<plugin-root>/scripts/token_usage.py" report --agents [transcript-path]

# Add per-model ↳ breakdown rows (also subsets of the parent row)
python3 "<plugin-root>/scripts/token_usage.py" report --models [transcript-path]

# Compare two transcripts — per-label cost and output deltas
python3 "<plugin-root>/scripts/token_usage.py" report --diff OLD.jsonl NEW.jsonl

# Machine-readable JSON
python3 "<plugin-root>/scripts/token_usage.py" json [transcript-path]

# JSON diff between two transcripts
python3 "<plugin-root>/scripts/token_usage.py" json --diff OLD.jsonl NEW.jsonl

# Cross-session history
python3 "<plugin-root>/scripts/token_usage.py" history [--by project|day|command|model] [--since 7d|DATE] [--project SUBSTR] [--json|--csv]

# Insights — rule-based checks, no LLM involved
python3 "<plugin-root>/scripts/token_usage.py" insights [transcript-path]     # session mode
python3 "<plugin-root>/scripts/token_usage.py" insights --since 7d|30d|DATE [--project SUBSTR]  # window mode
python3 "<plugin-root>/scripts/token_usage.py" insights --json [transcript-path]

# Costliest sessions or commands in a window
python3 "<plugin-root>/scripts/token_usage.py" top_consumers [--by session|command] [--since 30d] [--limit N]
```

- With no argument, `report` and `json` auto-discover the most recently modified session transcript for the current working directory's project (`~/.claude/projects/<cwd-slug>/*.jsonl`) — normally the live session. In **Cowork** (the Claude desktop app), where there is no Claude Code project for the cwd, discovery falls back to the read-only transcript mounted in the session sandbox (`<mount>/.claude/projects/…`, `/sessions/*/mnt/.claude/projects/…`). Failing both of those, it falls back further to the newest transcript under **any** project on the machine — so running `report`/`json`/`insights` from a directory with no Claude Code history of its own will analyse whatever project's session is most recent rather than reporting "not found". Pass an explicit transcript path (or `session_id` for the MCP tools) when it matters which session gets analysed.
- If the user supplied a path, treat it as the transcript path (a session's `.jsonl`) and pass it through.
- For `history`, `--since` accepts relative values (`7d`, `30d`) or ISO dates (`2026-06-01`). `--by` defaults to `project`. `--project` is a substring filter that composes with any `--by`. Relative `--since` windows append a burn-rate footer (avg $/day, projected $/week).
- For `insights`, pass a transcript for session mode OR `--since` for window mode — not both. Session mode checks cost outlier vs the 30-day project median, prompt-cache regression, ad-hoc-work dominance, unpriced models, agent fan-out concentration, and budget pace. Window mode checks spend trend, the top mover behind an increase, and unpriced models across the window. `--project` composes with `--since` the same way it does for `history`; it is a window-mode filter only, so passing it without `--since` is an error rather than a silently ignored flag.

A live ledger may also exist at `~/.cache/token-usage/<session-id>.json` (maintained by this plugin's Stop hook). Prefer running the script fresh — it is fast (~1s) and always current mid-turn; the ledger only updates at turn boundaries. The Stop hook is Claude-Code-only, so in Cowork there is no ledger — always run the script fresh.

## How to present the result

### For `report` (current session)

1. Show the markdown table the script prints, verbatim — it is already formatted (columns: Activity, Calls, Output, Input, Cache read, Cache write, Est. cost).
2. Add one or two sentences of interpretation: name the biggest consumer and anything notable (e.g. a single command dominating cost, heavy subagent fan-out, unusually low cache-read ratio).
3. Keep the script's pricing disclaimer line — costs are API-price estimates and subscription (Max/Pro) users are not billed per token.

### For `report --agents` / `report --models`

Show the full table including the ↳ indented rows. Clarify to the user that ↳ rows (agent types or models) are **subsets** of their parent row's totals — they do not add to the parent, they break it down. Use `--models` when the user asks which model consumed the tokens (e.g. Opus main loop vs Haiku subagents).

### For `report --diff` / `json --diff`

Show the diff output verbatim. Note that `—` in a delta column means one side had unresolvable model pricing — the tool deliberately avoids fabricating savings in that case.

### For `history`

Show the table verbatim. If the user asked about spending over a time period (e.g. "what did I spend this week"), use `--since 7d` and `--by day`. If asking about a specific project, use `--by project` (or `--project SUBSTR` to filter to it). If asking about command patterns, use `--by command`. If asking which models cost the most, use `--by model`. If asked for a spreadsheet/export, use `--csv`.

### For `insights`

Use session mode (no `--since`) when the user asks about the current or a specific past session (e.g. "why was this session expensive", "any tips on my token spend"). Use window mode (`--since 7d|30d|DATE`) when they ask about a period (e.g. "analyse my token usage this month").

1. Show the findings verbatim — each is a `- [warn|info] message` line, already worded for a human to read.
2. Add at most 1–2 sentences of interpretation on top (e.g. which finding is most actionable). Do not restate every line in prose.
3. Never invent a finding the tool didn't emit — if the tool says `No notable findings.`, say that plainly; it's a normal, healthy result, not a failure or something to explain away.
4. The all-clear says what was examined — pass that on rather than dropping it. `No sessions in window — nothing was scanned.` means the scan found no transcripts at all (check the window and `--project`), and a trailing `(baseline: N prior session(s); the comparison rules need 5)` means the cost-outlier and cache-regression rules were off for this project, so an expensive session could still have gone unremarked.

## Interpreting the columns

- **Activity** — a slash command (one row per command name, summed across invocations), a skill invoked via the Skill tool in Cowork (also shown as `/skill-name`), or `(no command)` for turns before the first command/skill in the session. `(+N agents)` means N subagent transcripts were rolled up into that row.
- **Output** — tokens the model generated; the dominant cost driver at 5× the input rate.
- **Cache read / Cache write** — prompt-cache traffic. Cache reads cost ~0.1× the input rate (0.025× on Fable 5.1 / Mythos 5.1); large cache-read numbers are normal for long sessions and much cheaper than they look.
- **Est. cost** — computed per model from the bundled pricing table (`data/pricing.json`) plus any user overlay, cache-aware (5m writes at 1.25×, 1h writes at 2×, reads at the model's cache-hit rate). `—` means the model was not in the pricing table.

## Troubleshooting

- "transcript not found: <path>": the path passed does not exist (typo, stale path, wrong machine) — check it before looking anywhere else.
- "no transcript found": nothing was passed and discovery found no transcripts at all — not under `~/.claude/projects` (Claude Code), nor a Cowork mount. Ask the user for the transcript path, or list `~/.claude/projects/` / `/sessions/*/mnt/.claude/projects/` to locate the right transcript.
- "no transcript found under … (project dir …)" from an MCP tool: the projects tree exists but *that* project has no sessions yet. An explicit project dir never falls back to another project's session — pass `transcript` or `session_id` for a session elsewhere.
- "TOKEN_USAGE_TRANSCRIPT is set to <path> but that file does not exist": the environment override points at a missing file; unset it or fix the path.
- Zero rows / empty table: the session has no assistant turns yet.
- Costs look ~2.5× too high vs `/cost`: the dedup-by-requestId logic failed — verify the transcript entries carry `requestId` fields and report the issue.
- `history` shows fewer sessions than expected: `--since` filters by the first timestamp in each transcript; sessions with no timestamps are skipped.
