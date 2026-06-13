# token-usage

**Where did my tokens go?** A Claude Code plugin that attributes token usage to the work that consumed it — per-slash-command breakdowns, subagent rollups, a live per-session ledger, cross-session history, and cache-aware cost estimates.

Claude Code tells you session totals (`/cost`, OTel metrics) and tools like ccusage aggregate by day/model — but nothing answers *"the PR review cost 120k tokens, the refactor cost 800k"*. token-usage fills that gap.

```
| Activity                      | Calls | Output | Input | Cache read | Cache write | Est. cost |
|-------------------------------|------:|-------:|------:|-----------:|------------:|----------:|
| `/code-review` (+5 agents)    |     1 | 180.2k |  3.1k |      42.3M |        1.2M |    $29.40 |
| (no command)                  |     4 |  31.7k |  9.9k |       3.5M |      244.5k |     $4.20 |
| `/commit`                     |     2 |   2.4k |  0.8k |     310.0k |       18.0k |     $0.27 |
| **Total**                     |       |  214k  | 13.8k |      46.1M |        1.5M |    $33.87 |
```

## Features

- **Per-command attribution** — a slash command owns every turn until the next command, so multi-turn exchanges stay attributed to the command that triggered them. `(no command)` covers only turns that occurred before the first command in the session.
- **Works in Cowork too** — in the Claude desktop app (Cowork), skills run mid-turn via the Skill tool rather than a `<command-name>` prompt; each gets its own sticky segment (e.g. `/pptx`, `/report`). Transcript discovery falls back to the Cowork sandbox mount when there's no Claude Code project directory for the cwd, so `report` just works in both.
- **Subagent rollup** — agents spawned during a command (sidechains under `<session>/subagents/`) count toward the command that spawned them, labelled `(+N agents)`.
- **Per-agent-type breakdown** — `report --agents` adds ↳ indented rows showing token usage by agent type (e.g. `↳ claude-code-guide`, `↳ general-purpose`). These rows are **subsets** of their parent row's totals, not additive — the parent already includes them all.
- **Cross-session history** — `history` rolls up token usage across all sessions, filterable by project, day (local time), or command. An incremental per-transcript cache (`~/.cache/token-usage/index/`) means transcripts re-parse only when they change; warm scans are near-instant.
- **Compare mode** — `report --diff OLD NEW` (and `json --diff`) shows per-label cost and output deltas between two transcripts. Deterministic ordering; when either side has unresolvable model pricing the delta renders as `—` rather than silently faking a saving.
- **Correct dedup** — Claude Code writes the same API request's usage to multiple transcript entries while streaming. token-usage dedups by `requestId`, keeping per-field maxima across duplicates (robust to partial snapshots); a naive sum overcounts ~2.5×.
- **Cache-aware cost estimates** — per-model pricing with cache reads at 0.1×, 5-minute cache writes at 1.25×, and 1-hour cache writes at 2× the input rate. Mixed-model sessions (e.g. Opus main loop + Haiku subagents) are priced per model, and reports show what prompt caching saved you. Bedrock (`us.anthropic.…`) and OpenRouter-style (`anthropic/…`) model IDs resolve too.
- **Budget nudges** — set `TOKEN_USAGE_BUDGET_USD` and the Stop hook emits a one-time `systemMessage` warning when the session's estimated cost crosses the threshold. At most one warning per session.
- **Live ledger** — a Stop hook keeps `~/.cache/token-usage/<session-id>.json` current after every turn, so reports are instant and a statusline can show running cost.

## Installation

```bash
# Test locally
claude --plugin-dir /path/to/token-usage

# Or install from a marketplace once published
/plugin install token-usage
```

Requires `python3` (3.9+, stdlib only — no dependencies).

## Usage

### `/token-usage:report`

Ask for a breakdown any time:

```
/token-usage:report
```

Or just ask naturally: *"where did my tokens go this session?"*

Pass a transcript path to analyse a past session:

```
/token-usage:report ~/.claude/projects/<project-slug>/<session-id>.jsonl
```

### CLI (outside Claude Code)

The parser is a standalone script:

```bash
# Current session — markdown table
python3 scripts/token_usage.py report [transcript.jsonl]

# Add per-agent-type ↳ breakdown rows (subsets of the parent row, not additive)
python3 scripts/token_usage.py report --agents [transcript.jsonl]

# Compare two transcripts — per-label cost and output deltas
python3 scripts/token_usage.py report --diff OLD.jsonl NEW.jsonl

# Machine-readable JSON
python3 scripts/token_usage.py json [transcript.jsonl]

# JSON diff between two transcripts
python3 scripts/token_usage.py json --diff OLD.jsonl NEW.jsonl

# Cross-session history
python3 scripts/token_usage.py history                         # all sessions, by project
python3 scripts/token_usage.py history --by day                # grouped by calendar day (local time)
python3 scripts/token_usage.py history --by command            # grouped by slash command
python3 scripts/token_usage.py history --since 7d              # last 7 days
python3 scripts/token_usage.py history --since 2026-06-01      # since a specific date
python3 scripts/token_usage.py history --by project --json     # machine-readable
```

With no argument `report` and `json` pick the most recent session for the current directory's project.

### Budget nudges

Set `TOKEN_USAGE_BUDGET_USD` in the environment Claude Code runs hooks with (e.g. the `env` block of `~/.claude/settings.json`):

```json
{
  "hooks": { "Stop": [{ "command": "..." }] },
  "env": { "TOKEN_USAGE_BUDGET_USD": "50" }
}
```

When the session's estimated cost crosses the threshold the Stop hook emits a `systemMessage` warning once per session. The same API-price-estimate disclaimer applies — this is a usage signal, not a billing alert.

### Statusline (optional)

`examples/statusline.sh` reads the live ledger and renders e.g. `⏶ 214k out · $33.87 · top: /code-review`. Wire it up with `/statusline` or merge it into your existing statusline script. Requires `jq`.

## How it works

Claude Code writes every session to `~/.claude/projects/<project-slug>/<session-id>.jsonl`. Each assistant entry carries full API usage (`input_tokens`, `output_tokens`, cache read/write, model). token-usage:

1. Streams the JSONL, deduplicating assistant entries by `requestId`.
2. Starts a new segment at each real user prompt; prompts carrying a `<command-name>` marker label the segment with that command. A command's label is sticky — it covers every subsequent turn until the next command.
3. Sums each subagent transcript (`<session-id>/subagents/agent-*.jsonl`) and attributes it to the segment active at the agent's start time.
4. Prices each model's usage against `data/pricing.json`.

In **Cowork** (the Claude desktop app) the same transcript format is mounted read-only inside the session sandbox under `<mount>/.claude/projects/…` (and `/sessions/*/mnt/.claude/projects/…`); discovery uses these when no Claude Code project matches the cwd. Skills there are invoked via a `Skill` tool_use block instead of a `<command-name>` prompt, so each such block opens its own sticky segment (deduped by tool-use id). The Stop hook is Claude-Code-only, so in Cowork run `report` on demand rather than relying on the live ledger.

The Stop hook (`hooks/hooks.json`) re-runs this after every turn and writes the result to `~/.cache/token-usage/<session-id>.json` (override the directory with `TOKEN_USAGE_LEDGER_DIR`). Hook failures never block the session.

The `history` subcommand builds an incremental index under `~/.cache/token-usage/index/`, keyed by file path and re-validated by (mtime, size). Transcripts are only re-parsed when their content changes; subsequent scans skip unchanged files and complete in milliseconds.

## Cost disclaimer

Costs are **API-price estimates** from the bundled `data/pricing.json` (rates as of June 2026). Subscription plans (Pro/Max) are not billed per token — treat the figure as "what this would cost at API prices". Update `data/pricing.json` if rates change; models not in the table show `—`.

## Limitations

- Older transcripts without `requestId` fields are summed without dedup (may overcount).
- `--since` filters sessions by their first timestamp; sessions whose transcripts carry no timestamps are skipped.
- Day buckets in `history --by day` use local time.

## License

MIT
