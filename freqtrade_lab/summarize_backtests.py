from __future__ import annotations

import csv
import json
import zipfile
from pathlib import Path


RESULTS_DIR = Path("user_data/backtest_results")


def strategy_payloads(zip_path: Path) -> dict:
    with zipfile.ZipFile(zip_path) as archive:
        result_name = next(name for name in archive.namelist() if name.endswith(".json") and "_config" not in name)
        payload = json.loads(archive.read(result_name))
    return payload["strategy"]


def row_for_result(zip_path: Path, strategy_name: str, payload: dict) -> dict:
    total_pair = next(row for row in payload["results_per_pair"] if row["key"] == "TOTAL")
    return {
        "file": zip_path.name,
        "strategy": strategy_name,
        "start": payload.get("backtest_start"),
        "end": payload.get("backtest_end"),
        "trades": payload.get("total_trades"),
        "profit_pct": round(payload.get("profit_total", 0) * 100, 4),
        "profit_abs": round(payload.get("profit_total_abs", 0), 4),
        "profit_factor": round(payload.get("profit_factor", 0), 4),
        "sharpe": round(payload["sharpe"], 4) if payload.get("sharpe") is not None else "",
        "sortino": round(payload["sortino"], 4) if payload.get("sortino") is not None else "",
        "winrate": round(total_pair.get("winrate", 0) * 100, 2),
        "max_drawdown_pct": round(total_pair.get("max_drawdown_account", 0) * 100, 4),
        "max_drawdown_abs": round(total_pair.get("max_drawdown_abs", 0), 4),
        "market_change_pct": round(payload.get("market_change", 0) * 100, 4),
    }


def main() -> int:
    rows = []
    for path in sorted(RESULTS_DIR.glob("*.zip")):
        for strategy_name, payload in strategy_payloads(path).items():
            rows.append(row_for_result(path, strategy_name, payload))

    rows.sort(key=lambda item: (item["sharpe"] or -999, item["profit_pct"]), reverse=True)
    if not rows:
        print("No backtest zip files found.")
        return 1

    fieldnames = list(rows[0])
    writer = csv.DictWriter(open("/dev/stdout", "w", newline=""), fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
