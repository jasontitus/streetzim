#!/bin/bash
# Records upload/failure events to upload-events.log, detached from any
# Claude session. The in-session Monitors die when the session ends; this
# does not. Append-only, deduplicated.
cd /storage/streetzim
OUT=/storage/streetzim/upload-events.log
touch "$OUT"
declare -A SEEN=()
while IFS= read -r l; do SEEN["$(printf '%s' "$l" | md5sum | cut -c1-10)"]=1; done < "$OUT"
while :; do
  for f in viewer-refresh.log continent-chain.log retrofit-chips-queue.log queue-continents.out; do
    [ -f "$f" ] || continue
    while IFS= read -r line; do
      case "$line" in
        *SHIPPED*|*"uploaded →"*|*"listing pending"*|*"GATES FAILED"*|*"UPLOAD FAILED"*|*"BUILD FAILED"*|*"finished rc="*)
          k=$(printf '%s' "$line" | md5sum | cut -c1-10)
          if [ -z "${SEEN[$k]:-}" ]; then
            SEEN[$k]=1
            printf '[%s] %s | %s\n' "$(date -Iseconds)" "$f" "$line" >> "$OUT"
          fi ;;
      esac
    done < <(tail -60 "$f" 2>/dev/null)
  done
  sleep 60
done
