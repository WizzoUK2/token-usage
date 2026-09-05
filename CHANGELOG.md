# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.6.0] — 2026-09-04

### Added

- **MCP server** — `scripts/mcp_server.py`, a stdlib stdio JSON-RPC 2.0 server
  registered by the plugin's new `.mcp.json` (Claude Code auto-starts it; Claude
  desktop can register the same script). Tools: `session_cost`, `history`,
  `insights`, `diff`, `top_consumers`; `format: json|markdown`; tool failures
  are `isError` results, protocol problems are JSON-RPC errors; nothing but
  JSON-RPC reaches stdout. Current session resolves from
  `TOKEN_USAGE_PROJECT_DIR` (`${CLAUDE_PROJECT_DIR}`), then the Cowork mount,
  then the newest transcript anywhere.
- **`top_consumers` subcommand** — costliest sessions (`--by session`) or
  command labels aggregated across sessions (`--by command`) in a window;
  `--since`, `--project`, `--limit`, `--json`. Unpriced rows sort last.
- **Session-id lookup** — `locate_transcript()` resolves a Claude Code session
  id across every project; `resolve_transcript()` now fails cleanly on a
  non-existent explicit path instead of crashing in the parser.
- **Pricing:** `claude-fable-5-1`, `claude-mythos-5-1` ($10/$50, cache hits
  $0.25/MTok) and `claude-opus-5` ($5/$25) in the bundled table. Fable 5.1
  usage previously fell through the prefix matcher to the `claude-fable-5`
  entry — right per-token rates, but cache reads overstated 4×. Legacy
  `claude-3-5-haiku` ($0.80/$4) added so old transcripts price too.
- **Per-model cache-hit rate** — pricing entries (bundled or user overlay)
  accept an optional `"cache_read"` ($/MTok). When present it replaces the
  0.1× input multiplier for that model in both cost and cache-savings
  figures; entries without it behave exactly as before. A non-numeric
  `cache_read` invalidates the entry (warned, skipped), like `input`/`output`.

### Changed

- **Transcript auto-discovery falls back further when there's no project
  context.** `report`, `json`, and `insights` used to return "no transcript
  found" when the cwd had no Claude Code project directory and no Cowork
  mount existed. They (and `find_latest_transcript()`, which the MCP server's
  "current session" resolution also uses) now fall back one step further to
  the newest transcript under *any* project on the machine. This is
  necessary for Claude desktop and MCP callers with no project context to
  anchor on, but it also means running these commands from a directory with
  no Claude Code history of its own can silently analyse a different
  project's most recent session instead of failing — pass an explicit
  transcript path or session id when that matters.
- **Sonnet 5 priced at $2/$10** (was $3/$15). Anthropic made the launch
  price permanent in September 2026 instead of raising it, so the "promo not
  modelled" caveat is gone. Sonnet 5 session costs drop by a third vs 0.5.0.

### Fixed

- **History index no longer serves stale costs after a pricing change.**
  Cached per-transcript summaries bake `cost_usd` in but were re-validated
  only by (mtime, size), so `history --by project|day|command` and the
  `insights` 30-day baseline kept pricing old rates until a transcript
  changed. Entries now also carry a fingerprint of the effective pricing
  table (bundled + overlay) and re-parse when it differs. Expect a one-off
  full re-scan on first run after upgrading.

## [0.5.0] — 2026-07-09

### Added

- **`insights` subcommand** — rule-based checks over token spend, pure
  arithmetic (no LLM, no network). Session mode (`insights [transcript]`)
  runs six rules: cost-outlier (warn ≥3× / info ≥2× the 30-day project
  median session cost; needs ≥5 prior sessions), cache-regression (warn on
  a ≥20 percentage-point cache-read-ratio drop vs a command's norm),
  adhoc-dominance (info at ≥50% of spend), unpriced-models (warn),
  agent-fanout (info at ≥70% of a command's cost from its subagents), and
  budget-pace (info at 75–100% of `TOKEN_USAGE_BUDGET_USD`). Window mode
  (`insights --since Nd|DATE [--project SUB]`) runs three rules:
  spend-trend (warn ≥+50% / info ≥±25% half-over-half), top-mover (≥30% of
  the increase), and unpriced-models. Prints `- [warn|info] message` lines,
  or exactly `No notable findings.` when nothing fires. `--json` for
  structured output.
- **User pricing overlay** — `~/.config/token-usage/pricing.json`
  (`XDG_CONFIG_HOME` respected) merges per-model-key over the bundled
  pricing table, letting you price models the plugin doesn't ship rates
  for yet without waiting on a release. Malformed overlay files warn once
  on stderr and are otherwise ignored — never fatal.
- **Unpriced-model footnotes** — `report`, `json`, and `history` now name
  any models they couldn't price in a footnote, pointing at the user
  overlay, instead of silently rendering `—` with no explanation.

### Changed

- **`history --by day` now splits sessions across every local day they
  touched** (previously a session's usage was attributed only to its
  start day). The `Calls` column for `--by day` now counts sessions
  touching that day. Daily figures for the same underlying data shift vs
  0.4.0 — same class of change as the 0.2.0 sticky-attribution rework.
  Index schema bumped to v3; existing caches re-parse once on the first
  scan after upgrading, then stay incremental.

## [0.4.0] — 2026-07-06

### Fixed

- **Pricing:** added `claude-sonnet-5` ($3/$15) and `claude-mythos-5`
  ($10/$50) to the bundled table — Sonnet 5 usage previously rendered `—`
  and was silently excluded from cost totals.
- Model-pricing prefix matching now stops at segment boundaries, so a
  future `claude-opus-4-10` falls through to the `claude-opus-4` family
  rate instead of mispricing against `claude-opus-4-1`.
- Project auto-discovery slugs the cwd with Claude Code's actual rule
  (every non-alphanumeric character → dash), fixing paths with spaces.

### Added

- `report --models` — per-model ↳ breakdown rows (subsets of the parent
  row); `json` output gains `models` arrays per label unconditionally.
- `history --by model` — cross-session rollup by model (calls = API
  requests). Index schema v2 caches per-model buckets; v1 entries
  re-parse once on the first scan after upgrading.
- `history --project SUBSTR` — substring filter composable with any `--by`.
- `history --csv` — raw-number CSV export.
- Burn-rate footer on `history --since Nd`: average $/day and projected
  $/week for the window.
- Budget nudges re-warn at each further multiple of
  `TOKEN_USAGE_BUDGET_USD` (2×, 3×, …); ledgers track
  `budget_notified_multiple` (legacy `budget_notified` bools read as
  1×-already-sent).
- The ledger hook also runs on `SubagentStop`, keeping the statusline
  fresh during long agent-heavy turns. (Measured: full parse of a 74MB
  transcript takes ~0.3s, so incremental parsing remains unnecessary.)
  SubagentStop payloads carry the subagent's own sidechain transcript, so
  the hook resolves the owning session transcript and re-aggregates the
  whole session; if it can't be found, it writes nothing rather than
  clobbering the ledger. Because SubagentStop hooks run in parallel,
  ledger temp files are per-process and budget nudges only fire (and only
  advance the multiple counter) from the serial Stop hook.

### Changed

- Budget nudge messages name the actual multiple crossed — a session
  first reporting in at $25 on a $10 budget says "2× your $10.00 budget".
  Non-positive `TOKEN_USAGE_BUDGET_USD` values are now inert (previously
  `0` warned once on any spend).
- `--since` values that are neither `Nd` nor `YYYY-MM-DD` now exit with
  an error instead of silently filtering out every session.
- `history --by model` labels its count column **Requests** (API
  requests), since the other groupings count sessions/invocations.

## [0.3.0] — 2026-06-13

### Added

- **Cowork support (Claude desktop app).** Transcript discovery now falls back
  to the Cowork sandbox mount (`<mount>/.claude/projects/…` and
  `/sessions/*/mnt/.claude/projects/…`) when no Claude Code project directory
  matches the cwd; Claude Code keeps priority when both are present.
- **Per-skill attribution.** Skills invoked mid-turn via the Skill tool (how
  Cowork runs them, rather than a `<command-name>` prompt) each get their own
  sticky segment, deduped by tool-use id so streamed duplicates don't reopen or
  double-count them.

## [0.2.0] — 2026-06-12

### Changed

- **Sticky attribution (behaviour change):** a slash command now owns every
  turn until the next command, so per-command numbers grow vs 0.1.x for the
  same transcript. `(no command)` covers only turns before the first command.

### Added

- `history` subcommand — cross-session rollups by project, day (local time),
  or command, with an incremental per-transcript cache (`--since 7d`, `--json`).
- `report --agents` — per-agent-type breakdown rows (subsets of the parent
  row); `json` output gains `agents` arrays per label and segment.
- `report|json --diff OLD NEW` — per-label cost/output deltas between two
  transcripts; deterministic ordering; unpriceable sides render as unknown.
- Budget nudges: set `TOKEN_USAGE_BUDGET_USD` and the Stop hook emits a
  one-time warning when the session's estimated cost crosses it.
- Test suite (pytest, 29 tests) and GitHub Actions CI (Python 3.9 + 3.12).

## [0.1.1] — 2026-06-12

### Changed

- Dedup hardened: streamed duplicates of one `requestId` now merge by
  per-field **maxima** instead of first-seen-wins, so partial usage
  snapshots can no longer undercount a request.
- Model pricing resolves provider-prefixed IDs: Bedrock
  (`us.anthropic.claude-…`) and OpenRouter/LiteLLM (`anthropic/claude-…`)
  formats map to the bundled rate table.

### Added

- Reports show estimated **prompt-caching savings** ("Prompt caching saved
  ~$X vs. full input rates"); `json` output gains `total.cache_savings_usd`.

## [0.1.0] — 2026-06-10

Initial release.

### Added

- Per-slash-command token attribution from Claude Code session transcripts,
  with an `(no command)` bucket so totals always reconcile.
- Subagent rollup — sidechain transcripts attributed to the command that
  spawned them, labelled `(+N agents)`.
- Deduplication by `requestId` (streamed transcript entries repeat usage; a
  naive sum overcounts ~2.5×).
- Cache-aware, per-model cost estimates (`data/pricing.json`): cache reads at
  0.1×, 5-minute cache writes at 1.25×, 1-hour writes at 2× the input rate.
- `/token-usage:report` skill (also answers "where did my tokens go?").
- Stop hook maintaining a live per-session ledger at
  `~/.cache/token-usage/<session-id>.json` (override with
  `TOKEN_USAGE_LEDGER_DIR`); never blocks the session.
- Standalone CLI: `python3 scripts/token_usage.py report|json [transcript]`.
- Optional statusline example (`examples/statusline.sh`, requires `jq`).

[Unreleased]: https://github.com/WizzoUK2/token-usage/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/WizzoUK2/token-usage/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/WizzoUK2/token-usage/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/WizzoUK2/token-usage/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/WizzoUK2/token-usage/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/WizzoUK2/token-usage/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/WizzoUK2/token-usage/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/WizzoUK2/token-usage/releases/tag/v0.1.0
