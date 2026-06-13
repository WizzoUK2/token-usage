# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/WizzoUK2/token-usage/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/WizzoUK2/token-usage/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/WizzoUK2/token-usage/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/WizzoUK2/token-usage/releases/tag/v0.1.0
