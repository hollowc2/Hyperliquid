# CLAUDE.md

Repo-wide notes. Per-project details live in `hl_engine/CLAUDE.md` and the folder READMEs.

## Machines: develop on zeus, run on athena

| Host | Role | Checkout |
|------|------|----------|
| **zeus** | Development: edit, test, backtest, commit | `/mnt/Repos/Trading/HyperLiquid` |
| **athena** | Runtime: long-running bots (Docker) | `~/hyperliquid` (user `corey`) |

Both are on Tailscale; `ssh athena` works from zeus with no extra config. Check `hostname` if unsure which box you're on.

Rules:
- **Don't run long-lived bots on zeus.** Backtests, tests, and quick one-off `docker compose run --rm` are fine.
- **Don't edit code on athena.** Its checkout is only updated by pushes from zeus. Local edits there will block the next push.
- **Athena is paper-only.** `freqtrade_lab/.env` on athena has `HL_PRIVATE_KEY` intentionally blank, and every running config is `dry_run: true`. Going live is a deliberate, separate decision.

## Deploy flow

```bash
# on zeus, after committing to main
scripts/deploy.sh                               # push origin + athena, recreate paper bots
FT_SERVICES="hype-paper" scripts/deploy.sh      # only some services
scripts/deploy.sh --no-restart                  # push code only
```

How it works: zeus has a git remote `athena` → `athena:hyperliquid`, a non-bare repo with
`receive.denyCurrentBranch=updateInstead`, so `git push athena main` updates athena's working tree directly.
Athena has no GitHub credentials and doesn't need any.

## What runs on athena

`freqtrade_lab/docker-compose.yml`, services `hype-paper` (FreqUI port 8081) and `funding-carry-paper` (port 8082).
They use `restart: unless-stopped` and Docker is enabled at boot, so they survive reboots with no systemd units.
The old `freqtrade` (StreakReversal, port 8080) service is not started.

```bash
ssh athena 'cd ~/hyperliquid/freqtrade_lab && docker compose ps'
ssh athena 'cd ~/hyperliquid/freqtrade_lab && docker compose logs -f --tail 50 hype-paper'
ssh -L 8081:localhost:8081 -L 8082:localhost:8082 athena   # FreqUI at http://localhost:8081
```

Ports are bound to `127.0.0.1` on athena, so use an SSH tunnel to reach FreqUI.

State that lives only on athena (gitignored, never pushed): `freqtrade_lab/.env`, `user_data/tradesv3-*.sqlite`,
`user_data/logs/`, `user_data/data/`. To change a secret, edit `.env` on athena, then `docker compose up -d --force-recreate <svc>`.

Not deployed yet: `hl_engine` (orchestrator / recorders / TimescaleDB). The `hl_engine/*.service` files reference
`/opt/hyperliquid` and user `billy`. They are stale and not installed anywhere, so update them before using them on athena.

Set up 2026-10-04; paper bots started fresh (zeus's July paper DBs left on zeus as an archive).
