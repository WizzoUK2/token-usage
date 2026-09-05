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
  JSON-RPC reaches stdout. "Current session" resolves in order: explicit
  `transcript` → `session_id` → `TOKEN_USAGE_TRANSCRIPT` → the newest session
  for `TOKEN_USAGE_PROJECT_DIR` (or `CLAUDE_PROJECT_DIR`), which fails closed
  rather than falling through to another project → and only when no project
  dir is set: the cwd's own project, the Cowork mount, then the newest
  transcript anywhere.
- **MCP results disclose how they were produced.** `session_cost` and
  `insights` carry `resolved_via` (`explicit`, `session_id`, `env`,
  `project_dir`, `cwd`, `cowork`, `any_project`) in JSON, and their markdown
  adds a note when the session was merely the newest one found (`cwd` /
  `any_project`). Every tool result carries a `warnings` list in JSON and a
  `Warning: <text>` footnote per warning in markdown, so pricing-overlay
  problems and a non-numeric `TOKEN_USAGE_BUDGET_USD` — stderr lines on the
  CLI, invisible over MCP — reach the caller. `TOKEN_USAGE_PROJECT_DIR` that
  is blank or still holds an unexpanded `${CLAUDE_PROJECT_DIR}` (Claude Code
  before 2.1.139, or another host) counts as unset and falls back to
  discovery instead of erroring on every call.
- **`top_consumers` subcommand** — costliest sessions (`--by session`) or
  command labels aggregated across sessions (`--by command`) in a window;
  `--since`, `--project`, `--limit`, `--json`. Unpriced rows sort last and are
  disclosed: session mode reports `unpriced_rows` for the window (with a
  footnote when `--limit` cut any of them), and command mode marks a label
  `"partial": true` whenever any of its usage ran on an unpriced model —
  including a single session that mixed a priced and an unpriced model under
  one label, where the per-session cost is a number and hides the gap — so a
  priced subtotal is never mistaken for the whole cost.
- **Session-id lookup** — `locate_transcript()` resolves a Claude Code session
  id across every project; `resolve_transcript()` now fails cleanly on a
  non-existent explicit path (`transcript not found: <path>`) instead of
  crashing in the parser.
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
  transcript path or session id when that matters. MCP callers only reach
  this fallback when no project dir is set; with one, a project that has no
  sessions yet is an error.
- **`report` and `insights` now name the transcript they measured.** The
  markdown `report` gains a `Session: <project-slug>/<session>.jsonl` line
  directly below the table (the cache-savings, unpriced and `Models:` lines
  still follow it), `insights` text output gains an extra
  `(session: <project-slug>/<session>.jsonl)` — appended as its own line when
  rules fired, and on the same line as `No notable findings.` when none did —
  and `insights --json` gains `transcript_path` in session mode.
  Auto-discovery can land on a session in a different project (see the
  fallback change above), so which one was analysed is no longer left
  implicit. Scripts parsing the text output should expect the extra line.
- **MCP argument checking is stricter and speaks its own vocabulary.** A bad
  window value now reads `invalid since value …` (the CLI still says
  `--since`); `since`/`project` reject the empty string; `budget_usd` must be
  greater than 0; `transcript` and `session_id` together, and `project`
  without `since` on `insights` (window mode is the only mode that filters by
  project), are rejected instead of one silently winning; and a float where an integer is required reads
  `limit must be an integer`. A handler that exits without a message reports
  `<tool>: exited with status <code>` and an unexpected exception now leaves a
  traceback on stderr for the host log.
- **`top_consumers --by command` on an empty window** says "No commands in
  window." rather than "No sessions in window.".
- **The `report` skill also allows the user-scope tool names**
  (`mcp__token-usage__*`) alongside the plugin's
  `mcp__plugin_token-usage_token-usage__*`, so a hand-registered server is
  usable from the skill.
- **CI runs `ruff check scripts tests`** (ruff's default rule set, no config
  file) alongside the pytest matrix.
- **Sonnet 5 priced at $2/$10** (was $3/$15). Anthropic made the launch
  price permanent in September 2026 instead of raising it, so the "promo not
  modelled" caveat is gone. Sonnet 5 session costs drop by a third vs 0.5.0.

### Fixed

- **A corpus scan no longer reports "no usage" as a success.** An unwritable
  cache directory (`TOKEN_USAGE_LEDGER_DIR` / `~/.cache/token-usage/index`)
  made `history`, `top_consumers`, window `insights` and the `insights`
  baseline skip every transcript and print an empty table; a single
  unreadable transcript disappeared the same way. Cache-write failures now
  warn once per process and the freshly parsed summary is used anyway, and an
  unreadable transcript is warned about on stderr, listed as
  `skipped_transcripts` in the JSON — at the top level in both `insights`
  modes, since a thinned baseline silently switches every session-mode rule
  off — and footnoted in the markdown.
- **`insights` no longer prints a clean bill of health when the rules could
  not run.** An empty findings list also covered a window scan that matched no
  sessions at all (a missing or mistyped `TOKEN_USAGE_PROJECTS_DIR`, a fresh
  machine, a `--project` substring matching no slug) and a session-mode
  baseline below the five prior sessions the comparison rules need — only
  `--json`'s `baseline.sessions` gave either away. The window case now reads
  `No sessions in window — nothing was scanned.` (matching `top_consumers`),
  and the session case appends `(baseline: N prior session(s); the comparison
  rules need 5)` after the session name. A window whose sessions all land
  after its midpoint — any window longer than the project's history — is
  disclosed the same way: the spend-trend and top-mover rules are gated on the
  first half holding spend, so it appends `(baseline: no spend in the window's
  first half; the trend rules need both halves)`, and `--json` carries
  `baseline.first_half_sessions` / `first_half_cost`. Both qualifiers are
  appended whether or not findings fired, since the rules that need no
  baseline (ad-hoc dominance, unpriced models) fire happily on a young project
  — a single `[info]` line was being handed over as the whole story for a
  10,000× session.
- **The Stop hook survives well-formed JSON that isn't a hook payload.**
  `null`, a list, a bare string or a non-string `transcript_path` on stdin got
  past the `JSONDecodeError` guard and then raised `AttributeError` /
  `TypeError` outside the broad catch, exiting 1 with a traceback — the one
  thing a hook must never do. Non-object payloads and non-string transcript
  paths now exit 0 in silence.
- **`insights --project` without `--since` is an error, not a silent
  no-op.** `insights --project other` (and the MCP `insights
  {"project": …}`) took the session-mode path with the filter dropped, so it
  answered about whatever session discovery found, quite possibly in another
  project, with no warning. Both now say `--project applies to window mode
  (use --since)`.
- **The Stop hook says when it could not write the ledger.** An unwritable
  `TOKEN_USAGE_LEDGER_DIR` / `~/.cache/token-usage` (read-only home, a
  root-owned directory, a full disk) killed the budget nudge with no output
  on any channel; the hook now warns on stderr
  (`token-usage: hook: ledger update failed: …`) and still exits 0.
- **A broken `TOKEN_USAGE_TRANSCRIPT` is diagnosed by name.** Both the CLI and
  the MCP server used to answer "no transcript found"; they now say
  `TOKEN_USAGE_TRANSCRIPT is set to <path> but that file does not exist`.
- **History index no longer serves stale costs after a pricing change.**
  Cached per-transcript summaries bake `cost_usd` in but were re-validated
  only by (mtime, size), so `history --by project|day|command` and the
  `insights` 30-day baseline kept pricing old rates until a transcript
  changed. Entries now also carry a fingerprint of the effective pricing
  table (bundled + overlay) and re-parse when it differs. Expect a one-off
  full re-scan on first run after upgrading — `INDEX_VERSION` is also 3→4 in
  this release, so per-label entries can record whether any of their usage was
  unpriced.
- **The Stop hook survives any bytes at all on stdin.** `json.load(sys.stdin)`
  decodes before it parses, so a non-UTF-8 byte raised `UnicodeDecodeError` —
  a `ValueError`, but not a `json.JSONDecodeError` — straight past the guard:
  exit 1 with a traceback on every Stop/SubagentStop, and the ledger plus
  budget nudge silently dead for the rest of the session. The payload is now
  read as bytes and decoded as UTF-8 (which is what JSON is) with
  `errors="replace"`, whatever the locale says, so a POSIX-locale
  launchd/cron/container run also stops dying on an accented transcript path;
  and the whole hook body — diagnostics included — is wrapped so no exception
  class can escape. The nudge is computed before the ledger is written and is
  now emitted even when that write fails.
- **A few undecodable bytes in a transcript no longer traceback.**
  Transcripts, cache entries, ledgers, pricing layers and the plugin manifest
  are read as UTF-8 with `errors="replace"`, so `report`, `json`, `insights`,
  the hook and the corpus scan all survive a corrupt line instead of exiting
  1 on it. The MCP server reads its stdin the same way: one 0xff byte used to
  kill the process with rc=1 and *zero* replies, losing valid requests queued
  ahead of it, where it is now an ordinary `-32700` parse error.
- **A missing projects directory is disclosed, not answered with zeros.**
  `Path.glob()` on a non-existent path — or on a regular file — yields
  nothing rather than raising, so a mistyped `TOKEN_USAGE_PROJECTS_DIR`, an
  MCP server started with a different `HOME` or an unmounted sandbox answered
  "what did I spend this week" with a clean, successful, empty table. Every
  corpus-scanning entry point (`history`, `top_consumers`, window `insights`,
  the `insights` baseline) now reports `projects_dir_missing` in `--json`,
  footnotes `No Claude Code projects directory at <path> — nothing was
  scanned.` in markdown, warns on stderr and passes it to MCP `warnings`.
- **A calendar-invalid `--since` is rejected instead of crashing.**
  `--since 2026-09-31` (September has 30 days; likewise `2026-02-30`,
  `2025-02-29`, `2026-13-45`, `2026-09-01 lunchtime`) passed the shape check
  and then died inside `insights` window mode with a raw
  `ValueError: day is out of range for month`, while `history` took the same
  value and silently matched nothing. Both now say
  `invalid --since value '…' — use Nd (e.g. 7d) or YYYY-MM-DD` (`invalid
  since value …` over MCP).
- **Pricing rates must be finite and non-negative.** `json.loads` accepts the
  bare `NaN`/`Infinity` literals (and `1e400` overflows to `inf`), so a user
  overlay carrying one produced a `"cost_usd": NaN` in the MCP JSON payload —
  not RFC-8259 JSON, unparseable by a strict client — and in the Stop-hook
  ledger, and made the hook's `int(cost // limit)` raise on every Stop, which
  killed the budget nudge behind a single stderr line. Such entries are now
  warned about and skipped like any other invalid rate; `0` remains legal for
  free tiers.
- **`TOKEN_USAGE_BUDGET_USD=0` (or negative, or `nan`) says so.** It parsed
  fine and then silently switched off both the hook's nudge and the
  `insights` budget-pace rule, leaving the user believing budget monitoring
  was armed. It is now reported —
  `ignoring TOKEN_USAGE_BUDGET_USD='0' — must be > 0` — like a non-numeric
  typo, on stderr and in MCP `warnings`.
- **The unwritable-cache warning reaches MCP callers.** It was stderr-only
  and once per process, so a long-lived server re-parsed the whole corpus on
  every query in silence. It now travels in `warnings` for every call while
  staying once-per-process on stderr.
- **MCP rejects `NaN`/`Infinity` numbers and a non-object `arguments`.**
  `NaN` compares False against every bound, so `{"budget_usd": NaN}` ran the
  tool with the budget rule silently disabled; and `arguments` of `[]`, `0`,
  `""` or `false` was coerced to `{}`, answering about a session discovery
  guessed at instead of reporting the malformed call (now `-32602`; a missing
  or null `arguments` still means "no arguments").
- **The window caveat can no longer contradict the findings above it.**
  `insights --since` rounded the first half's cost to 6dp for the payload but
  the trend rules ran on the unrounded value, so a first half holding under
  ~$5e-7 (a few cache-read tokens) printed a spend-trend finding directly
  above "(baseline: no spend in the window's first half)". The window is now
  split once and shared with the rules, `--json` carries
  `baseline.first_half_spend` (the rules' own predicate) and the qualifier
  keys off it — and distinguishes `no sessions in the window's first half`
  from `no spend`, which the old wording could not.
- **`top_consumers` marks the rows its footnote is about.** A session or
  command whose cost is only a priced subtotal now renders `$2.00*` and is
  counted in `N session(s)/command(s) partially priced (marked *)`; session
  rows gained the `partial` flag command rows already had, and the count no
  longer includes rows that are wholly unpriced and already show `—`. A
  session mixing a priced and an unpriced model used to rank above an honest
  row with twice the tokens and the same figure, with nothing to show for it.
- **CI runs the suite the way the contributor guide does** — `-W error` on
  both interpreters, with `ruff` pinned to the version used locally.

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
