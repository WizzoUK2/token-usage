---
name: report
description: Generate a per-command breakdown of Claude Code token usage and estimated API cost for the current session, including subagent rollups. This skill should be used when the user asks "where did my tokens go", "token usage report", "how many tokens did that command use", "what did this session cost", or "which command/subagent used the most tokens".
argument-hint: "[transcript-path]"
allowed-tools: Bash, Read
version: 0.1.0
---

# token-usage report

Produce a per-activity token-usage breakdown for the current Claude Code session: which slash commands consumed tokens, how much ad-hoc (non-command) work consumed, subagent rollups, and an estimated API-equivalent cost.

## How to run

The parser script lives at `../../scripts/token_usage.py` relative to this skill's base directory (i.e. `<plugin-root>/scripts/token_usage.py`). Resolve the plugin root from the "Base directory for this skill" path shown above, then run:

```bash
python3 "<plugin-root>/scripts/token_usage.py" report [transcript-path]
```

- With no argument, the script auto-discovers the most recently modified session transcript for the current working directory's project (`~/.claude/projects/<cwd-slug>/*.jsonl`) — normally the live session.
- If the user supplied a path, treat it as the transcript path (a session's `.jsonl`) and pass it through.
- For machine-readable output (e.g. the user wants to post-process), use the `json` subcommand instead of `report`.

A live ledger may also exist at `~/.cache/token-usage/<session-id>.json` (maintained by this plugin's Stop hook). Prefer running the script fresh — it is fast (~1s) and always current mid-turn; the ledger only updates at turn boundaries.

## How to present the result

1. Show the markdown table the script prints, verbatim — it is already formatted (columns: Activity, Calls, Output, Input, Cache read, Cache write, Est. cost).
2. Add one or two sentences of interpretation: name the biggest consumer and anything notable (e.g. a single command dominating cost, heavy subagent fan-out, unusually low cache-read ratio).
3. Keep the script's pricing disclaimer line — costs are API-price estimates and subscription (Max/Pro) users are not billed per token.

## Interpreting the columns

- **Activity** — a slash command (one row per command name, summed across invocations) or `(no command)` for ad-hoc conversational work. `(+N agents)` means N subagent transcripts were rolled up into that row.
- **Output** — tokens the model generated; the dominant cost driver at 5× the input rate.
- **Cache read / Cache write** — prompt-cache traffic. Cache reads cost ~0.1× the input rate; large cache-read numbers are normal for long sessions and much cheaper than they look.
- **Est. cost** — computed per model from the bundled pricing table (`data/pricing.json`), cache-aware (5m writes at 1.25×, 1h writes at 2×). `—` means the model was not in the pricing table.

## Troubleshooting

- "no transcript found": the cwd does not map to a Claude Code project directory. Ask the user for the transcript path, or list `~/.claude/projects/` to locate the right project slug.
- Zero rows / empty table: the session has no assistant turns yet.
- Costs look ~2.5× too high vs `/cost`: the dedup-by-requestId logic failed — verify the transcript entries carry `requestId` fields and report the issue.
