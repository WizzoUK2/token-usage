# Marketplace submission — token-usage

Submission target: Anthropic community plugin directory (`anthropics/claude-plugins-official`),
via the plugin directory submission form: https://clau.de/plugin-directory-submission

---

## Form answers — page 1 (fields confirmed from the live form)

Copy-paste ready, in the order the form asks them.

### Plugin homepage

> *"The public homepage or documentation site for your plugin."*

```
https://discovery.wickedsick.com/token-usage-claude-code-plugin-documentation
```

(Kept current for v0.4.0. Fallback if a repo URL is preferred:
`https://github.com/WizzoUK2/token-usage`.)

### Plugin name

> *"You should check your name is not already taken."*

```
token-usage
```

### Plugin description

> *"A clear, concise description of what your plugin does."*

```
A token profiler, not another cost meter. Claude Code tells you how much a
session cost; token-usage tells you what the work was: it parses the session
transcript and attributes every token to the slash command, skill, or subagent
that consumed it — "the code review cost 180k tokens, the ad-hoc work cost
30k" — with subagent fan-outs rolled up into the command that spawned them and
a "(no command)" bucket so totals always reconcile.

Key features: per-command/per-skill attribution (works in Claude Code and
Cowork); report breakdowns by agent type (--agents) and model (--models);
cross-session history by project, day, command, or model with a --project
filter, CSV export, and a burn-rate footer; --diff compare mode for
before/after prompt optimisation; a live per-session ledger maintained by
Stop/SubagentStop hooks powering an optional statusline segment and opt-in
budget nudges; and cache-aware per-model cost estimates (cache reads 0.1x,
5-minute cache writes 1.25x, 1-hour writes 2x), clearly labelled as API-price
estimates for subscription users. Python 3.9+ stdlib only — no dependencies,
no network calls, no telemetry.
```

### Example use cases

> *"Provide examples of how users can use your plugin."* (format: `Example 1: ...\nExample 2: ...`)

```
Example 1: "Where did my tokens go this session?" — at the end of a long session, run /token-usage:report (or just ask in natural language) and get a per-command table: instantly see the code review consumed 6x everything else combined.
Example 2: Deciding whether a heavy workflow is worth it — multi-agent commands (deep code reviews, research fan-outs) are powerful but expensive. token-usage shows their true deduped, cache-aware cost, so "run on every PR" vs "reserve for releases" is decided with real numbers.
Example 3: Profiling slash commands and skills you author — see exactly what each invocation costs (cache-write amplification and subagent fan-out included), break it down by agent type with --agents or by model with --models, and compare before/after with --diff when optimising prompts.
Example 4: "Which project ate the tokens this week?" — history --by project --since 7d (or by day, command, or model) rolls up every session on the machine, with a burn-rate footer and CSV export; an incremental cache keeps warm runs near-instant.
Example 5: Live cost awareness while you work — the optional statusline segment renders "214k out · $33.87 · top: /code-review", updated every turn from the live ledger; set TOKEN_USAGE_BUDGET_USD for nudges when a session crosses your budget (re-warned at each multiple).
Example 6: Auditing a past or headless session — the standalone CLI analyses any transcript outside Claude Code, with JSON output for dashboards or CI cost tracking of claude -p automation.
```

---

## Held in reserve — for later form pages (fields not yet seen)

The form has at least one more page; everything below is kept so answers are
ready whatever it asks.

### Proposed marketplace.json entry

```json
{
  "name": "token-usage",
  "description": "A token profiler, not another cost meter: attributes Claude Code usage to the slash command, skill, or subagent that consumed it — one row per activity, agent fan-outs rolled up, cache-aware per-model cost estimates, live per-session ledger, and cross-session history. Answers \"what did the work cost?\" at a granularity /cost and daily aggregators can't.",
  "author": {
    "name": "Craig Fletcher"
  },
  "category": "productivity",
  "source": {
    "source": "url",
    "url": "https://github.com/WizzoUK2/token-usage.git",
    "sha": "ee04f6245ce264cb752d328e6d34b72425ff90e9"
  },
  "homepage": "https://github.com/WizzoUK2/token-usage"
}
```

### Repository

https://github.com/WizzoUK2/token-usage

### Author / contact

Craig Fletcher — craigfletcheruk@gmail.com

### Category

productivity

### Short description (one line)

A token profiler, not a cost meter — attributes usage to the slash command, skill, or subagent that consumed it.

### Long description

Every existing tool answers *how much* — `/cost` gives session totals,
statuslines show a running number, aggregators roll up by day or model.
token-usage answers *what the work was*: it parses the session transcript and
produces one row per slash command or skill — "the code review cost 180k
tokens, the ad-hoc work cost 30k" — with subagent transcripts rolled up into
the command that spawned them and a `(no command)` bucket so totals always
reconcile.

Attribution is sticky (a command owns every turn until the next one) and works
in Cowork too, where skills run via the Skill tool rather than slash-command
prompts. Beyond the per-session report: `history` rolls up across all sessions
by project, day, command, or model, with a `--project` filter, `--csv` export,
and a burn-rate footer (incremental cache, near-instant warm scans); `--diff`
compares two transcripts per label for before/after prompt optimisation;
`--agents` and `--models` break a command down by agent type or model. Stop
and SubagentStop hooks maintain a live per-session ledger
(`~/.cache/token-usage/`) powering instant reports, an optional statusline
segment, and opt-in budget nudges that re-warn at each budget multiple.
Cost estimates are per-model and cache-aware (cache reads 0.1x, 5-minute cache
writes 1.25x, 1-hour writes 2x input rate), clearly labelled as API-price
estimates for subscription users.

### Components

- 1 skill: `/token-usage:report` (user-invoked; also triggers on "where did my tokens go")
- 1 hook: Stop hook updating the session ledger (command type, 15s timeout, never blocks)
- 1 script: `scripts/token_usage.py` — `report`/`json`/`history`/`hook` subcommands, Python 3.9+ stdlib only, no dependencies
- Optional statusline example (`examples/statusline.sh`, requires jq)

### Technical notes for reviewers

- Correctness: transcript entries repeat the same API request's usage across
  multiple streamed entries; the parser deduplicates by `requestId`, merging
  duplicates by per-field maxima so partial streamed snapshots can't
  undercount (a naive sum overcounts ~2.5x). Mixed-model sessions (e.g. Opus
  main loop + Haiku subagents) are priced per model; provider-prefixed IDs
  (Bedrock `us.anthropic.…`, OpenRouter `anthropic/…`) resolve too.
- Quality: 51-test pytest suite covering dedup, segmentation, subagent
  rollup, pricing, budget nudges, history caching, and diff; GitHub Actions
  CI on Python 3.9 and 3.12 (green on every commit).
- Security/privacy: reads only local Claude Code transcripts
  (`~/.claude/projects/`, plus the read-only Cowork sandbox mount), writes
  only to `~/.cache/token-usage/`. No network calls, no telemetry, no
  credentials, no third-party services. Hook failures are swallowed (exit 0)
  so the plugin can never block a session. Session IDs are sanitised before
  being used in ledger filenames.
- Tested on real sessions including a 15-subagent session (1,000+ deduped
  requests), plus headless verification via `claude -p --plugin-dir`.

### License

MIT (LICENSE file in repo)

### Documentation link

https://discovery.wickedsick.com/token-usage-claude-code-plugin-documentation
(The GitHub README https://github.com/WizzoUK2/token-usage#readme is the
canonical fallback.)

### Example use cases (long-form originals)

1. **"Where did my tokens go this session?"** — at the end of a long session,
   run `/token-usage:report` (or ask in natural language) and get a
   per-command table: instantly see the code review consumed 6× everything
   else combined.
2. **Deciding whether a heavy workflow is worth it** — multi-agent commands
   (deep code reviews, research fan-outs) are powerful but expensive.
   token-usage shows their true deduped, cache-aware cost, so "run on every
   PR" vs "reserve for releases" is decided with real numbers.
3. **Profiling slash commands and skills you author** — plugin developers see
   exactly what each invocation costs (cache-write amplification, subagent
   fan-out included), break it down by agent type with `--agents`, and
   compare before/after with `--diff` when optimising prompts.
4. **Which project ate the tokens this week?** — `history --by project
   --since 7d` (or by day, or by command) rolls up every session on the
   machine, with an incremental cache so warm runs are near-instant.
5. **Live cost awareness while you work** — the optional statusline segment
   renders `⏶ 214k out · $33.87 · top: /code-review`, updated every turn
   from the live ledger; set `TOKEN_USAGE_BUDGET_USD` for a one-shot nudge
   when a session crosses your threshold.
6. **Auditing a past or headless session** — the standalone CLI analyses any
   transcript outside Claude Code, with JSON output for dashboards or CI
   cost tracking of `claude -p` automation.
7. **Sanity-checking subagent-heavy sessions** — dozens of agents is exactly
   where naive counting fails (~2.5× overcount); the `requestId` dedup is
   validated on a 15-subagent session with 1,000+ deduped requests.
