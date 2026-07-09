# token-usage 0.5.0 — "Insights" release design

**Date:** 2026-07-09
**Status:** Approved design, pending implementation plan
**Baseline:** v0.4.0 (commit `ee04f62`, 51 tests, submitted to the Anthropic plugin directory 2026-07-06)

## Context and roadmap position

v0.4.0 completed the "measurement" arc: per-command/skill attribution, per-agent and
per-model breakdowns, cross-session history with filters/CSV/burn rate, budget
multiples, and a Stop/SubagentStop live ledger. The plugin collects richer data than
it interprets.

Agreed release sequencing:

- **0.5.0 — Insights (this spec):** rule-based interpretation of the data, user
  pricing overlay, honest per-day history.
- **0.6.0 — MCP server:** bundled stdlib stdio MCP server exposing cost queries as
  tools (`session_cost`, `history`, `diff`, `top_consumers`), registered via the
  plugin's `.mcp.json`. Primary motivation: Cowork has no hooks/ledger; MCP gives it
  structured access. Not designed here.
- **0.7.0 — Dashboard:** self-contained static-HTML dashboard (inline SVG, no CDN)
  generated from the history index, plus a `--live` auto-refresh terminal mode. Not
  designed here.

Drivers (in priority order): directory adoption, deeper analysis, new surfaces.
Explicitly not a driver: Craig's fleet/multi-machine aggregation.

## Hard constraints

- **Python 3.9+ stdlib only. Zero dependencies.** This is a directory selling point.
- No LLM calls, no network, no telemetry. Insights are pure arithmetic.
- Hook paths must never block or fail a session; a broken user config file must be
  survivable.
- All in the existing single script `scripts/token_usage.py`.

## Feature 1 — `insights` subcommand

### CLI

```
insights [transcript]                      # session mode (auto-discovery like report)
insights --since Nd|DATE [--project SUB]   # window mode
insights --json                            # structured output, both modes
```

### Engine

- A **finding** is `{rule, severity, message, data}` where severity ∈
  {`warn`, `info`}, `message` is one plain-English sentence stating what happened
  *and what to do about it*, and `data` carries the justifying numbers.
- A **rule** is a pure function `(session_agg, baseline) -> [finding]` (session
  mode) or `(window_summaries) -> [finding]` (window mode).
- The engine (not each rule) handles insufficient data: baseline-dependent rules are
  skipped silently when the project has fewer than **5 prior sessions** in the
  trailing 30 days. A fresh install emits nothing rather than nonsense.
- Zero findings is a valid result: print `No notable findings.` and exit 0.
- Rendering: findings ranked warn-first, one line each plus a one-line data
  justification. `--json` emits `{"findings": [...], "baseline": {...}}`.

### Baseline

Computed once per run from the existing history index (no new transcript scanning):
per-project median session cost, and per-command median cost + median cache-read
ratio, over the trailing 30 days. Warm index ⇒ near-instant.

### Session-mode rules

| # | Rule | Threshold | Severity | Example message |
|---|------|-----------|----------|-----------------|
| 1 | Cost outlier | ≥3× project 30d median = warn; ≥2× = info | warn/info | "This session ($41.20) is 4.1× your 30-day median for this project ($10.05)." |
| 2 | Cache hygiene regression | command's cache-read ratio ≥20 percentage points below its 30d norm | warn | "Cache-read ratio for /code-review dropped 92% → 61% — something is invalidating your prompt cache between turns." |
| 3 | Ad-hoc dominance | `(no command)` ≥50% of session cost | info | "58% of spend was ad-hoc work — wrap repeated workflows in a command to make them trackable." |
| 4 | Unpriced models | any usage whose model resolves no rates | warn | "claude-foo-6 is unpriced — add rates to ~/.config/token-usage/pricing.json (costs currently understated)." |
| 5 | Agent fan-out concentration | subagents ≥70% of a command's cost | info | "82% of /code-review's cost was its 8 subagents (top: general-purpose)." |
| 6 | Budget pace | >75% of `TOKEN_USAGE_BUDGET_USD` consumed (only when set) | info | "Session at $8.10 of your $10.00 budget — the Stop hook will nudge at $10.00." |

Cache-read ratio (rules 2 and baseline) = `cache_read / (cache_read + input + cache_write)`.

### Window-mode rules

| # | Rule | Threshold | Severity |
|---|------|-----------|----------|
| 7 | Spend trend (second half of window vs first half) | ≥50% increase; ≥25% change either way | warn / info |
| 8 | Top mover | single command/project explaining ≥30% of the increase | info |
| 9 | Window-wide unpriced models | as rule 4, aggregated | warn |

### Thresholds

All thresholds are named constants in one commented block (`INSIGHT_THRESHOLDS`
region). Tunable by maintainers in later releases; **not** user-configurable in
0.5.0 (YAGNI). Shipping criterion for any rule: it must state an action, not just
an observation.

## Feature 2 — User pricing overlay + unpriced-model visibility

- `load_pricing()` merges three layers, **per model key**:
  in-script defaults ← bundled `data/pricing.json` ←
  `$XDG_CONFIG_HOME/token-usage/pricing.json` (default
  `~/.config/token-usage/pricing.json`).
- Overlay schema is identical to the bundled file (including optional cache-write
  multiplier fields). Overlaying one model does not hide others.
- Malformed overlay (unparseable JSON, non-numeric rates): one stderr warning,
  overlay skipped, never fatal — the Stop hook must survive it.
- Unpriced-model detection: model IDs whose `rates_for()` resolution fails,
  deduped, exposed as `agg["unpriced_models"]`; `json` output includes it; the
  ledger written by the hook carries it.
- `report` and `history` append one footnote line only when the list is non-empty:
  `N model(s) unpriced (claude-foo-6): add rates to ~/.config/token-usage/pricing.json`.

## Feature 3 — Per-day session splitting (`history --by day`)

- During parsing, additionally bucket each deduped request's tokens + cost by
  **local date** of its timestamp (consistent with current `--by day` semantics).
- Requests with no timestamp fall into the transcript's first known date; a
  transcript with no timestamps at all is skipped from since-filtering (unchanged).
- Index entry gains `by_day: {"YYYY-MM-DD": {output, input, cache_read,
  cache_write, cost, requests}}`. `INDEX_VERSION` 2→3; v2 entries re-parse once on
  first scan (same proven migration path as v1→v2).
- `history --by day` sums day buckets across transcripts. The Sessions column
  counts sessions **touching** that day, so a midnight-spanning session is counted
  in both days while its cost splits honestly.
- Burn-rate and cache-savings footers are unchanged (they operate on window sums).
- **Behaviour change to CHANGELOG (Changed):** historical daily figures shift
  (more honest, less comparable with previously quoted numbers) — same treatment
  as the 0.2.0 sticky-attribution change.

## Skill and docs

- `SKILL.md`: add insight triggers ("any tips on my token spend", "analyse my token
  usage", "why was this session expensive") and presentation guidance: run
  `insights`, show findings verbatim, add at most 1–2 sentences of interpretation;
  never invent findings the tool didn't emit.
- README: feature bullets + an Insights section with example output; pricing
  overlay documented under configuration; per-day change noted in Limitations
  (replacing the "whole session books to first day" caveat).
- CHANGELOG 0.5.0 entry; Notion docs page updated **after** release (established
  workflow).

## Testing

Strict TDD (superpowers), extending the existing pytest suite (51 tests):

- Per rule: fire and no-fire cases on both sides of each threshold, using synthetic
  aggregates; skip-when-thin-baseline case.
- Engine: ranking, zero-findings output, `--json` shape.
- Overlay: three-layer merge precedence, single-model overlay doesn't mask bundled
  table, malformed overlay non-fatal + stderr warning, XDG_CONFIG_HOME respected.
- Unpriced detection: footnote renders only when non-empty; ledger carries list.
- Day split: midnight-spanning synthetic transcript splits cost and double-counts
  Sessions correctly; timestampless requests fall to first date; v2→v3 index
  migration re-parses once and round-trips.
- Expected: roughly 20–25 new tests.

## Release procedure

1. `git fetch` and check `git ls-remote --tags origin` + `origin/main` divergence
   before versioning (0.3.0 lesson — other fleet sessions may have shipped).
2. Version bump to 0.5.0 in `plugin.json`, `SKILL.md`, README; CHANGELOG dated.
3. Tag `v0.5.0`, push, CI green.
4. Directory follow-up: the marketplace entry pins `ee04f62` (v0.4.0); once listed,
   0.5.0 requires Anthropic to bump the pinned SHA via the directory's update
   process — installs stay on 0.4.0 until then.

## Risks and mitigations

- **Horoscope risk** (findings that sound smart but aren't actionable): shipping
  criterion + zero-findings-is-valid + thin-baseline skip.
- **Threshold bikeshedding:** constants in one block, defaults chosen from Craig's
  real transcript corpus during implementation; adjust empirically before release.
- **Index migration cost:** one cold re-parse of the ~1,800-transcript corpus on
  first scan after upgrading (warm scans measured 0.36s in v0.4.0; the equivalent
  v1→v2 migration was unremarkable). Acceptable one-off.
- **Hook regression risk:** overlay loading and unpriced tracking touch the hook
  path; covered by non-fatal handling + existing hook tests.

## Out of scope (deferred)

- MCP server (0.6.0), dashboard/live mode (0.7.0).
- User-configurable rule thresholds.
- Fleet/multi-machine aggregation; LLM-generated insights; intro/promotional
  pricing windows (e.g. Sonnet 5 intro pricing) — static table + overlay only.
