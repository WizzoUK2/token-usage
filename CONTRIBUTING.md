# Contributing to token-usage

Thanks for your interest! Bug reports, fixes, and focused features are all
welcome.

## Ground rules

- **Zero dependencies.** `scripts/token_usage.py` is Python 3.9+ standard
  library only — that's what makes the plugin installable everywhere with no
  setup. PRs that add third-party imports (to the parser or the hook path)
  won't be accepted.
- **Never block the session.** The Stop hook must always exit 0. Anything
  that can fail (missing transcript, malformed JSONL, unwritable cache dir)
  must fail silently or degrade gracefully.
- **Local-only.** No network calls, no telemetry. The plugin reads
  `~/.claude/projects/` and writes `~/.cache/token-usage/` — nothing else.
- **Totals must reconcile.** Any attribution change must keep the invariant
  that segment rows sum to the session total. Dedup by `requestId` is
  load-bearing — see "Correct dedup" in the README before touching it.

## Dev setup

```bash
git clone https://github.com/WizzoUK2/token-usage.git
claude --plugin-dir /path/to/token-usage   # run Claude Code with the plugin
```

No build step. The parser runs standalone:

```bash
python3 scripts/token_usage.py report                      # latest session, current project
python3 scripts/token_usage.py report path/to/session.jsonl
python3 scripts/token_usage.py json  path/to/session.jsonl
```

## Testing a change

There is no formal test suite yet (contributions welcome — pytest, stdlib
fixtures only). Until then, verify against real transcripts:

1. Run `report` against a few of your own transcripts in
   `~/.claude/projects/<project-slug>/`, including at least one session that
   spawned subagents (`<session-id>/subagents/agent-*.jsonl` exists).
2. Check the table total against Claude Code's own `/cost` for the same
   session — they should agree on output tokens (small drift on cache
   numbers is expected while a session is still open).
3. For hook changes, verify headlessly:
   `claude -p "hi" --plugin-dir .` then confirm
   `~/.cache/token-usage/<session-id>.json` was written and is valid JSON.

## Pricing updates

`data/pricing.json` holds per-model API rates. Rate-update PRs are the
easiest contribution there is: update the numbers, note the source and date
in the PR description. Models missing from the table render as `—` rather
than guessing.

## Style

- Match the existing code: small functions, type hints, no classes unless
  state genuinely demands one.
- Keep the parser streaming — transcripts can be hundreds of MB; never load
  the whole file into memory.
- One change per PR. Refactors separate from behaviour changes.

## Releases

Versioning is semver in `.claude-plugin/plugin.json`; user-visible changes
get a line in `CHANGELOG.md` under Unreleased, which is rolled into a
version heading at release time.

## Reporting issues

Use GitHub Issues. For attribution bugs, the single most useful thing you
can attach is a **redacted** transcript snippet (the `usage` blocks and
`requestId`s, not your conversation content) plus the table you got vs the
table you expected. For anything security-relevant, see `SECURITY.md`.
