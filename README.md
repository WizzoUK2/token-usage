# token-usage

**Where did my tokens go?** A Claude Code plugin that attributes token usage to the work that consumed it — per-slash-command breakdowns, subagent rollups, a live per-session ledger, and cache-aware cost estimates.

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

- **Per-command attribution** — every turn started by a slash command is attributed to that command; everything else lands in `(no command)`. Totals always reconcile.
- **Subagent rollup** — agents spawned during a command (sidechains under `<session>/subagents/`) count toward the command that spawned them, labelled `(+N agents)`.
- **Correct dedup** — Claude Code writes the same API request's usage to multiple transcript entries while streaming. token-usage dedups by `requestId`; a naive sum overcounts ~2.5×.
- **Cache-aware cost estimates** — per-model pricing with cache reads at 0.1×, 5-minute cache writes at 1.25×, and 1-hour cache writes at 2× the input rate. Mixed-model sessions (e.g. Opus main loop + Haiku subagents) are priced per model.
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
python3 scripts/token_usage.py report [transcript.jsonl]   # markdown table
python3 scripts/token_usage.py json   [transcript.jsonl]   # machine-readable
```

With no argument it picks the most recent session for the current directory's project.

### Statusline (optional)

`examples/statusline.sh` reads the live ledger and renders e.g. `⏶ 214k out · $33.87 · top: /code-review`. Wire it up with `/statusline` or merge it into your existing statusline script. Requires `jq`.

## How it works

Claude Code writes every session to `~/.claude/projects/<project-slug>/<session-id>.jsonl`. Each assistant entry carries full API usage (`input_tokens`, `output_tokens`, cache read/write, model). token-usage:

1. Streams the JSONL, deduplicating assistant entries by `requestId`.
2. Starts a new segment at each real user prompt; prompts carrying a `<command-name>` marker label the segment with that command.
3. Sums each subagent transcript (`<session-id>/subagents/agent-*.jsonl`) and attributes it to the segment active at the agent's start time.
4. Prices each model's usage against `data/pricing.json`.

The Stop hook (`hooks/hooks.json`) re-runs this after every turn and writes the result to `~/.cache/token-usage/<session-id>.json` (override the directory with `TOKEN_USAGE_LEDGER_DIR`). Hook failures never block the session.

## Cost disclaimer

Costs are **API-price estimates** from the bundled `data/pricing.json` (rates as of June 2026). Subscription plans (Pro/Max) are not billed per token — treat the figure as "what this would cost at API prices". Update `data/pricing.json` if rates change; models not in the table show `—`.

## Limitations (v1)

- Attribution granularity is the slash command. Work after a command, in the same conversational thread but a new user turn, counts as `(no command)`.
- A command's segment ends at the next user prompt — long multi-turn commands attribute only their first turn's work to the command label.
- Current-session scope only; cross-session history is on the roadmap.
- Older transcripts without `requestId` fields are summed without dedup (may overcount).

## License

MIT
