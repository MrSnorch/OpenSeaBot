#!/usr/bin/env bash
# Хранение состояния бота в репозитории: ветка `state` с одним файлом state.json (один коммит, история не копится).
#   scripts/state.sh pull   — достать state.json из ветки и собрать state.db
#   scripts/state.sh push   — выгрузить state.db в state.json и сохранить в ветку
set -euo pipefail
BRANCH=state
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REMOTE="${STATE_REMOTE:-https://x-access-token:${GH_TOKEN:-}@github.com/${GITHUB_REPOSITORY:-}.git}"

case "${1:-}" in
  pull)
    rm -f state.tmp state.json.tmp
    if git fetch --depth=1 "$REMOTE" "$BRANCH" 2>/dev/null; then
      if git show FETCH_HEAD:state.json > state.json.tmp 2>/dev/null; then
        rm -f state.db
        python3 "$HERE/state_json.py" load state.json.tmp state.db
        rm -f state.json.tmp
        echo "state restored from state.json (branch '$BRANCH')"
        exit 0
      elif git show FETCH_HEAD:state.db > state.tmp 2>/dev/null; then  # старый формат
        mv state.tmp state.db
        echo "state restored from legacy state.db (branch '$BRANCH')"
        exit 0
      fi
    fi
    rm -f state.tmp state.json.tmp
    echo "no saved state yet — starting fresh"
    ;;
  push)
    if [ ! -f state.db ]; then
      echo "state.db not found in $(pwd) — nothing to save yet"
      exit 0
    fi
    tmp="$(mktemp -d)"
    python3 "$HERE/state_json.py" dump state.db "$tmp/state.json"
    echo "saving state.json ($(stat -c %s "$tmp/state.json") bytes) to branch '$BRANCH'..."
    if (
      cd "$tmp"
      git init -q
      git config user.name "github-actions[bot]"
      git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
      git add state.json
      git commit -qm "state $(date -u +%FT%TZ)"
      GIT_TERMINAL_PROMPT=0 git push -f "$REMOTE" "HEAD:refs/heads/$BRANCH" 2>&1 | sed "s#x-access-token:[^@]*@#x-access-token:***@#"
      exit "${PIPESTATUS[0]}"
    ); then
      echo "OK: state.json saved to branch '$BRANCH' at $(date -u +%FT%TZ)"
    else
      echo "ERROR: could not push state.json. If you see 403: Settings -> Actions -> General -> Workflow permissions -> 'Read and write permissions'." >&2
      rm -rf "$tmp"
      exit 1
    fi
    rm -rf "$tmp"
    ;;
  *)
    echo "usage: $0 pull|push" >&2
    exit 2
    ;;
esac
