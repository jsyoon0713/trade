#!/bin/bash

WATCH_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Watching $WATCH_DIR for changes..."

fswatch -o --exclude="\.git" "$WATCH_DIR" | while read -r; do
  cd "$WATCH_DIR"
  if [[ -n $(git status --porcelain) ]]; then
    git add -A
    git commit -m "auto: $(date '+%Y-%m-%d %H:%M:%S')"
    git push
    echo "Pushed at $(date '+%Y-%m-%d %H:%M:%S')"
  fi
done
