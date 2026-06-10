#!/bin/bash
# token-usage statusline example — shows session cost + top consumer in the Claude Code statusline.
#
# Setup: point your statusline at this script (or merge into an existing one):
#   /statusline  →  "command": "bash /path/to/token-usage/examples/statusline.sh"
#
# Reads the per-session ledger maintained by token-usage's Stop hook
# (~/.cache/token-usage/<session_id>.json). Requires jq.

set -euo pipefail

input=$(cat)
session_id=$(echo "$input" | jq -r '.session_id // empty')
ledger="${TOKEN_USAGE_LEDGER_DIR:-$HOME/.cache/token-usage}/${session_id}.json"

if [[ -z "$session_id" || ! -f "$ledger" ]]; then
  echo "token-usage: no ledger yet"
  exit 0
fi

jq -r '
  def fmt: if . >= 1000000 then "\(. / 1000000 * 10 | round / 10)M"
           elif . >= 1000 then "\(. / 1000 * 10 | round / 10)k"
           else tostring end;
  (.total.usage.output | fmt) as $out
  | (if .total.cost_usd != null then "$\(.total.cost_usd * 100 | round / 100)" else "?" end) as $cost
  | (.by_label | to_entries | sort_by(-(.value.cost_usd // 0)) | first) as $top
  | "⏶ \($out) out · \($cost)" + (if $top then " · top: \($top.key)" else "" end)
' "$ledger"
