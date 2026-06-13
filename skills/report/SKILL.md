---
name: report
description: Generate a per-activity breakdown of Claude Code or Cowork token usage and estimated API cost for the current or a past session, attributing usage to slash commands (Claude Code) or skills (Cowork), including subagent rollups, per-agent-type breakdown, cross-session history, compare mode, and budget nudge status. This skill should be used when the user asks "where did my tokens go", "token usage report", "how many tokens did that command or skill use", "what did this session cost", "which command/skill/subagent used the most tokens", "show me token history", "what did I spend this week", "token history by day/project/command", or "compare token usage between two sessions".
argument-hint: "[transcript-path]"
allowed-tools: Bash, Read
version: 0.3.0
---

# token-usage report

Produce a per-activity token-usage breakdown for the current Claude Code session: which slash commands consumed tokens, how much ad-hoc (non-command) work consumed, subagent rollups, and an estimated API-equivalent cost. Also handles cross-session history and transcript comparison.

## How to run

The parser script lives at `../../scripts/token_usage.py` relative to this skill's base directory (i.e. `<plugin-root>/scripts/token_usage.py`). Resolve the plugin root from the "Base directory for this skill" path shown above, then run:

```bash
# Current session — markdown table
python3 "<plugin-root>/scripts/token_usage.py" report [transcript-path]

# Add per-agent-type ↳ breakdown rows (subsets of parent row, not additive)
python3 "<plugin-root>/scripts/token_usage.py" report --agents [transcript-path]

# Compare two transcripts — per-label cost and output deltas
python3 "<plugin-root>/scripts/token_usage.py" report --diff OLD.jsonl NEW.jsonl

# Machine-readable JSON
python3 "<plugin-root>/scripts/token_usage.py" json [transcript-path]

# JSON diff between two transcripts
python3 "<plugin-root>/scripts/token_usage.py" json --diff OLD.jsonl NEW.jsonl

# Cross-session history
python3 "<plugin-root>/scripts/token_usage.py" history [--by project|day|command] [--since 7d|DATE] [--json]
```

- With no argument, `report` and `json` auto-discover the most recently modified session transcript for the current working directory's project (`~/.claude/projects/<cwd-slug>/*.jsonl`) — normally the live session. In **Cowork** (the Claude desktop app), where there is no Claude Code project for the cwd, discovery falls back to the read-only transcript mounted in the session sandbox (`<mount>/.claude/projects/…`, `/sessions/*/mnt/.claude/projects/…`), so no argument is needed there either.
- If the user supplied a path, treat it as the transcript path (a session's `.jsonl`) and pass it through.
- For `history`, `--since` accepts relative values (`7d`, `30d`) or ISO dates (`2026-06-01`). `--by` defaults to `project`.

A live ledger may also exist at `~/.cache/token-usage/<session-id>.json` (maintained by this plugin's Stop hook). Prefer running the script fresh — it is fast (~1s) and always current mid-turn; the ledger only updates at turn boundaries. The Stop hook is Claude-Code-only, so in Cowork there is no ledger — always run the script fresh.

## How to present the result

### For `report` (current session)

1. Show the markdown table the script prints, verbatim — it is already formatted (columns: Activity, Calls, Output, Input, Cache read, Cache write, Est. cost).
2. Add one or two sentences of interpretation: name the biggest consumer and anything notable (e.g. a single command dominating cost, heavy subagent fan-out, unusually low cache-read ratio).
3. Keep the script's pricing disclaimer line — costs are API-price estimates and subscription (Max/Pro) users are not billed per token.

### For `report --agents`

Show the full table including the ↳ indented agent-type rows. Clarify to the user that ↳ rows are **subsets** of their parent row's totals — they do not add to the parent, they break it down.

### For `report --diff` / `json --diff`

Show the diff output verbatim. Note that `—` in a delta column means one side had unresolvable model pricing — the tool deliberately avoids fabricating savings in that case.

### For `history`

Show the table verbatim. If the user asked about spending over a time period (e.g. "what did I spend this week"), use `--since 7d` and `--by day`. If asking about a specific project, use `--by project`. If asking about command patterns, use `--by command`.

## Interpreting the columns

- **Activity** — a slash command (one row per command name, summed across invocations), a skill invoked via the Skill tool in Cowork (also shown as `/skill-name`), or `(no command)` for turns before the first command/skill in the session. `(+N agents)` means N subagent transcripts were rolled up into that row.
- **Output** — tokens the model generated; the dominant cost driver at 5× the input rate.
- **Cache read / Cache write** — prompt-cache traffic. Cache reads cost ~0.1× the input rate; large cache-read numbers are normal for long sessions and much cheaper than they look.
- **Est. cost** — computed per model from the bundled pricing table (`data/pricing.json`), cache-aware (5m writes at 1.25×, 1h writes at 2×). `—` means the model was not in the pricing table.

## Troubleshooting

- "no transcript found": the cwd does not map to a Claude Code project directory and no Cowork mount was found. Ask the user for the transcript path, or list `~/.claude/projects/` (Claude Code) / `/sessions/*/mnt/.claude/projects/` (Cowork) to locate the right transcript.
- Zero rows / empty table: the session has no assistant turns yet.
- Costs look ~2.5× too high vs `/cost`: the dedup-by-requestId logic failed — verify the transcript entries carry `requestId` fields and report the issue.
- `history` shows fewer sessions than expected: `--since` filters by the first timestamp in each transcript; sessions with no timestamps are skipped.
