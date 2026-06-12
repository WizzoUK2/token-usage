# Marketplace submission — token-usage

Submission target: Anthropic community plugin directory (`anthropics/claude-plugins-official`),
via the plugin directory submission form: https://clau.de/plugin-directory-submission

## Proposed marketplace.json entry

```json
{
  "name": "token-usage",
  "description": "Attribute Claude Code token usage to the work that consumed it — per-slash-command breakdowns with subagent rollups, a live per-session ledger, and cache-aware API cost estimates. Answers \"where did my tokens go?\" at a granularity that session totals and daily aggregators don't.",
  "author": {
    "name": "Craig Fletcher"
  },
  "category": "productivity",
  "source": {
    "source": "url",
    "url": "https://github.com/WizzoUK2/token-usage.git",
    "sha": "d5eb6b0e12949fe58ad5ddb566cd1c9680820881"
  },
  "homepage": "https://github.com/WizzoUK2/token-usage"
}
```

## Form answers

**Plugin name:** token-usage

**Repository:** https://github.com/WizzoUK2/token-usage

**Author / contact:** Craig Fletcher — craigfletcheruk@gmail.com

**Category:** productivity

**Short description (one line):**
Per-command token usage attribution for Claude Code — where did my tokens go?

**Long description:**
Claude Code reports session totals (`/cost`) and community tools aggregate by
day or model, but nothing attributes usage to *what the session was doing*.
token-usage parses the session transcript and answers "the code review cost
180k tokens, the ad-hoc work cost 30k": one row per slash command, subagent
transcripts rolled up into the command that spawned them, and an `(no
command)` bucket so totals always reconcile. A Stop hook maintains a live
per-session ledger (`~/.cache/token-usage/`) that powers instant reports and
an optional statusline segment showing running cost. Cost estimates are
per-model and cache-aware (cache reads at 0.1x, 5-minute cache writes at
1.25x, 1-hour writes at 2x input rate), clearly labelled as API-price
estimates for subscription users.

**Components:**
- 1 skill: `/token-usage:report` (user-invoked; also triggers on "where did my tokens go")
- 1 hook: Stop hook updating the session ledger (command type, 15s timeout, never blocks)
- 1 script: `scripts/token_usage.py` — Python 3.9+ stdlib only, no dependencies
- Optional statusline example (`examples/statusline.sh`, requires jq)

**Technical notes for reviewers:**
- Correctness: transcript entries repeat the same API request's usage across
  multiple streamed entries; the parser deduplicates by `requestId` (a naive
  sum overcounts ~2.5x). Mixed-model sessions (e.g. Opus main loop + Haiku
  subagents) are priced per model.
- Security/privacy: reads only local Claude Code transcripts
  (`~/.claude/projects/`), writes only to `~/.cache/token-usage/`. No network
  calls, no telemetry, no credentials, no third-party services. Hook failures
  are swallowed (exit 0) so the plugin can never block a session. Session IDs
  are sanitised before being used in ledger filenames.
- Tested on real sessions including a 15-subagent session (1,000+ deduped
  requests), plus headless verification via `claude -p --plugin-dir`.

**License:** MIT (LICENSE file in repo)

**Documentation link:**
https://discovery.wickedsick.com/token-usage-claude-code-plugin-documentation
(The GitHub README https://github.com/WizzoUK2/token-usage#readme is the
canonical fallback.)

**Example use cases:**

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
   fan-out included) and can compare before/after when optimising prompts.
4. **Live cost awareness while you work** — the optional statusline segment
   renders `⏶ 214k out · $33.87 · top: /code-review`, updated every turn
   from the live ledger.
5. **Auditing a past or headless session** — the standalone CLI analyses any
   transcript outside Claude Code, with JSON output for dashboards or CI
   cost tracking of `claude -p` automation.
6. **Sanity-checking subagent-heavy sessions** — dozens of agents is exactly
   where naive counting fails (~2.5× overcount); the `requestId` dedup is
   validated on a 15-subagent session with 1,000+ deduped requests.
