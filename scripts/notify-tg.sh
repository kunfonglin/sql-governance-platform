#!/usr/bin/env bash
# notify-tg.sh — 推送訊息到 Telegram
#
# 用法:
#   TG_BOT_TOKEN=xxx TG_CHAT_ID=yyy ./notify-tg.sh "message"
#   或
#   ./notify-tg.sh --token xxx --chat-id yyy --message "..."
#
# 訊息支援 Markdown V2（見 https://core.telegram.org/bots/api#markdownv2-style）
# 若訊息含特殊字元，請呼叫者自行 escape。

set -euo pipefail

TOKEN="${TG_BOT_TOKEN:-}"
CHAT_ID="${TG_CHAT_ID:-}"
MESSAGE=""
PARSE_MODE="MarkdownV2"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --token)      TOKEN="$2"; shift 2 ;;
    --chat-id)    CHAT_ID="$2"; shift 2 ;;
    --message)    MESSAGE="$2"; shift 2 ;;
    --parse-mode) PARSE_MODE="$2"; shift 2 ;;
    --plain)      PARSE_MODE=""; shift ;;
    *)
      if [[ -z "$MESSAGE" ]]; then
        MESSAGE="$1"; shift
      else
        echo "Unknown arg: $1" >&2; exit 64
      fi
      ;;
  esac
done

[[ -z "$TOKEN"   ]] && { echo "ERROR: TG_BOT_TOKEN not set" >&2; exit 64; }
[[ -z "$CHAT_ID" ]] && { echo "ERROR: TG_CHAT_ID not set"   >&2; exit 64; }
[[ -z "$MESSAGE" ]] && { echo "ERROR: message required"     >&2; exit 64; }

URL="https://api.telegram.org/bot${TOKEN}/sendMessage"

if [[ -n "$PARSE_MODE" ]]; then
  curl -sS -X POST "$URL" \
    --data-urlencode "chat_id=${CHAT_ID}" \
    --data-urlencode "text=${MESSAGE}" \
    --data-urlencode "parse_mode=${PARSE_MODE}" \
    --data-urlencode "disable_web_page_preview=true" \
    > /dev/null
else
  curl -sS -X POST "$URL" \
    --data-urlencode "chat_id=${CHAT_ID}" \
    --data-urlencode "text=${MESSAGE}" \
    --data-urlencode "disable_web_page_preview=true" \
    > /dev/null
fi

echo "✓ TG message sent"
