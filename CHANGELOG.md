# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/WizzoUK2/token-usage/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/WizzoUK2/token-usage/releases/tag/v0.1.0
