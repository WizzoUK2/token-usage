# Security Policy

## Scope

token-usage runs locally inside Claude Code. It reads Claude Code transcripts
(`~/.claude/projects/`) and writes a per-session ledger
(`~/.cache/token-usage/`). It makes **no network calls** and bundles **no
credentials**. The main security-relevant surfaces are:

- the Stop hook command executed by Claude Code (`hooks/hooks.json`)
- filesystem path handling (session IDs are sanitised before being used in
  ledger filenames)
- the optional statusline script (`examples/statusline.sh`), which shells out
  to `jq`

## Supported versions

Only the latest release on `main` is supported with fixes.

## Reporting a vulnerability

Please **do not** open a public issue for security problems. Instead use
GitHub's private vulnerability reporting on this repository
(Security → Report a vulnerability), or email
**craigfletcheruk@gmail.com** with `[token-usage security]` in the subject.

You can expect an acknowledgement within a few days. Please include
reproduction steps and your environment (OS, Claude Code version, Python
version).
