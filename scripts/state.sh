#!/usr/bin/env bash
# Хранение state.db в репозитории: отдельная ветка `state` из одного коммита (история не копится).
#   scripts/state.sh pull   — достать state.db из ветки (если она есть)
#   scripts/state.sh push   — сохранить текущий state.db в ветку
set -euo pipefail
BRANCH=state
REMOTE="${STATE_REMOTE:-https://x-access-token:${GH_TOKEN:-}@github.com/${GITHUB_REPOSITORY:-}.git}"

case "${1:-}" in
  pull)
    if git fetch --depth=1 "$REMOTE" "$BRANCH" 2>/dev/null && git show FETCH_HEAD:state.db > state.tmp 2>/dev/null; then
      mv state.tmp state.db
      echo "state.db restored from branch '$BRANCH'"
    else
      rm -f state.tmp
      echo "no saved state yet — starting fresh"
    fi
    ;;
  push)
    [ -f state.db ] || { echo "no state.db to save"; exit 0; }
    tmp="$(mktemp -d)"
    # согласованная копия, даже если бот прямо сейчас пишет в базу
    python3 - "$tmp/state.db" <<'PY'
import sqlite3, sys
src = sqlite3.connect("state.db", timeout=30)
dst = sqlite3.connect(sys.argv[1])
src.backup(dst)
dst.close(); src.close()
PY
    (
      cd "$tmp"
      git init -q
      git config user.name "github-actions[bot]"
      git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
      git add state.db
      git commit -qm "state $(date -u +%FT%TZ)"
      git push -qf "$REMOTE" "HEAD:refs/heads/$BRANCH"
    )
    rm -rf "$tmp"
    echo "state.db saved to branch '$BRANCH'"
    ;;
  *)
    echo "usage: $0 pull|push" >&2
    exit 2
    ;;
esac
