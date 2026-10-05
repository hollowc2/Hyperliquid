#!/usr/bin/env bash
# Deploy committed main from zeus (dev) to athena (runtime). See CLAUDE.md.
#
#   scripts/deploy.sh                 # push + recreate default paper bots
#   FT_SERVICES="hype-paper" scripts/deploy.sh
#   scripts/deploy.sh --no-restart    # push code only
set -euo pipefail

FT_SERVICES="${FT_SERVICES:-hype-paper funding-carry-paper}"
REMOTE_DIR="hyperliquid"
RESTART=1
[[ "${1:-}" == "--no-restart" ]] && RESTART=0

cd "$(git rev-parse --show-toplevel)"

if [[ "$(git branch --show-current)" != "main" ]]; then
    echo "deploy: not on main" >&2; exit 1
fi
if [[ -n "$(git status --porcelain)" ]]; then
    echo "deploy: working tree dirty; commit first" >&2; exit 1
fi

git push origin main
git push athena main

if (( RESTART )); then
    # --force-recreate so strategy/config edits in the bind-mounted user_data are picked up.
    ssh athena "cd ~/$REMOTE_DIR/freqtrade_lab && docker compose up -d --force-recreate $FT_SERVICES && docker compose ps"
fi

ssh athena "git -C ~/$REMOTE_DIR log --oneline -1"
