# token-usage 0.2.0 — design spec

**Date:** 2026-06-12
**Status:** Approved design, pre-implementation
**Baseline:** v0.1.1 (`96a7967`)

Six features for the 0.2.0 release: sticky multi-turn attribution,
cross-session history, budget nudges, agent-type granularity, compare mode,
and a test suite with CI. All live in the existing single-script parser
(`scripts/token_usage.py`, Python 3.9+ stdlib only at runtime).

## Decisions made during brainstorm

| Fork | Choice |
|---|---|
| Multi-turn attribution | **Command owns everything until the next command.** No flag for the old behaviour. |
| History data source | **Scan `~/.claude/projects/` with an incremental per-transcript cache** (complete history, fast after first run) — not ledger-only. |
| Budget configuration | **`TOKEN_USAGE_BUDGET_USD` env var, cost-based**, one nudge per session. |

## 1. Sticky attribution

**Current:** a command's segment ends at the next user prompt, so follow-up
turns continuing the command's work land in `(no command)` — the README's
headline limitation.

**New:** a segment ends only when another slash-command prompt starts (or the
transcript ends). `(no command)` covers only turns before the first command
of the session.

- Implementation: in `parse_session`, only `<command-name>`-bearing user
  prompts call `new_segment`; plain user prompts no longer do. The
  `OTHER_LABEL` segment is created lazily for pre-command turns exactly as
  today.
- Subagent rollup is unaffected (agents already attach to the segment active
  at their start time, which is now the sticky segment).
- **Behaviour change:** reported per-command numbers grow vs 0.1.x for the
  same transcript. Changelog calls this out prominently; README's
  Limitations section drops the first two bullets and documents the sticky
  rule instead.

## 2. Cross-session history

New subcommand:

```
python3 scripts/token_usage.py history [--by project|day|command] [--since 7d|30d|YYYY-MM-DD] [--json]
```

- **Scan:** walk `~/.claude/projects/*/<session>.jsonl` (and their
  `subagents/` dirs via the existing rollup). Respect
  `TOKEN_USAGE_PROJECTS_DIR` override for tests.
- **Incremental cache:** per-transcript aggregate stored at
  `~/.cache/token-usage/index/<sha1-of-path>.json` containing
  `{path, mtime, size, parsed_at, segments-summary, by_model}`. A transcript
  re-parses only when `(mtime, size)` differ; the active session (mtime
  recent) always re-parses. Cache version field allows schema evolution;
  mismatched versions re-parse.
- **Output:** markdown table (or `--json`) rolled up by project directory
  name, ISO day (from each session's first timestamp, local time), or
  command label. Columns match `report` (calls, output, input, cache read,
  cache write, est. cost). Totals row always present.
- **`--since`:** filters sessions by first timestamp. Relative (`7d`)
  or absolute ISO date.
- First full scan prints a progress line to stderr every 25 files so large
  machines (hundreds of MB of JSONL) don't look hung. stdout stays clean for
  piping.
- The Stop-hook path does NOT build or update the history index (sessions
  must never pay the scan cost); only the `history` subcommand touches it.

## 3. Budget nudges

- Config: `TOKEN_USAGE_BUDGET_USD` (float, e.g. `50`). Unset/invalid → off.
  Read from the hook process environment (settable in `~/.claude/settings.json`
  `env` block or the shell).
- On each Stop-hook run, after writing the ledger: if estimated session cost
  ≥ budget AND the ledger lacks `budget_notified: true`, print
  `{"systemMessage": "token-usage: session estimate $X has passed your
  $Y budget — top consumer: <label>"}` to stdout and set
  `budget_notified: true` in the ledger.
- Exactly one nudge per session. Never blocks (exit 0 always); if cost is
  unpriceable (unknown model) the feature is inert.
- Documented in README under the statusline section with the same
  "API-price estimate" disclaimer.

## 4. Agent-type granularity

- Subagent transcripts carry the agent type; the rollup records a per-type
  counter and per-type usage alongside the existing per-segment totals.
  (Implementation detail: read the type from the agent transcript's metadata
  entry; fall back to `agent` when absent — verify the exact field against
  real fixtures during implementation, and prefer `unknown`-safe handling.)
- `report --agents` adds indented breakdown rows under each segment that has
  subagents:

```
| /code-review (+5 agents)   | ... |
|   ↳ Explore ×3             | ... |
|   ↳ general-purpose ×2     | ... |
```

- `json` output gains `segments[].agents: [{type, count, usage, cost_usd}]`
  unconditionally (cheap, no flag needed).
- Default markdown table is unchanged without the flag.

## 5. Compare mode

```
python3 scripts/token_usage.py report --diff OLD.jsonl NEW.jsonl
```

- Parses both transcripts, joins segments by label, renders one table:
  per-label columns for A est. cost, B est. cost, and Δ (cost and output
  tokens), plus a totals row. Labels present on only one side render with
  `—` on the missing side.
- Segment ordering: by absolute Δ cost descending.
- `--diff` combines with `--json` (`{a, b, delta_by_label}`); incompatible
  with `--agents` in v0.2.0 (error out clearly).

## 6. Tests + CI

- `tests/` with pytest (dev-only dependency; runtime stays stdlib-only).
  Synthetic JSONL fixtures built by small helpers, covering:
  - dedup maxima vs partial streamed snapshots (incl. missing `requestId`)
  - segmentation: sticky rule, pre-command `(no command)`, command labels
  - subagent rollup incl. agent types and orphan agents
  - provider-prefixed model pricing (`us.anthropic.…`, `anthropic/…`)
  - cost + cache-savings math against hand-computed values
  - budget nudge: fires once, respects unset/invalid env, never raises
  - history: cache hit on unchanged file, invalidation on mtime/size change,
    `--since` filtering, rollup keys
  - diff: join, missing-side labels, ordering
- CI: `.github/workflows/test.yml` — pytest on Python 3.9 and 3.12, push +
  PR on `main`. No secrets. (Repo's first CI; same shape as the
  spark_email_mcp pilot.)

## Out of scope (0.2.0)

- OTel/CSV export, burn-rate projections, repeat budget warnings,
  heuristic continuation detection, ledger-based history, Windows statusline
  parity (`statusline.sh` stays bash+jq).

## Sequencing

1. Test scaffolding + fixtures asserting **current** 0.1.1 behaviour; CI up.
2. Sticky attribution (update tests first — TDD).
3. Agent-type granularity.
4. Budget nudges.
5. Compare mode.
6. History (largest; last so the cache schema can reuse the final
   aggregate shapes).
7. Docs pass (README, CHANGELOG 0.2.0) + version bump + tag.
