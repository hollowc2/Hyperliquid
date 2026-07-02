from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Copy spot OHLCV JSON into Hyperliquid-style futures filenames."
    )
    parser.add_argument("--source-dir", default="user_data/data/coinbase", type=Path)
    parser.add_argument("--dest-dir", default="user_data/data/coinbase_hlproxy", type=Path)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--timeframes", nargs="+", default=["5m", "1h"])
    parser.add_argument("--source-quote", default="USD")
    parser.add_argument("--target-quote", default="USDC")
    parser.add_argument("--manifest", default="user_data/data/coinbase_hlproxy/manifest.json", type=Path)
    return parser.parse_args()


def source_name(symbol: str, quote: str, timeframe: str) -> str:
    return f"{symbol.upper()}_{quote.upper()}-{timeframe}.json"


def target_name(symbol: str, quote: str, timeframe: str) -> str:
    return f"{symbol.upper()}_{quote.upper()}_{quote.upper()}-{timeframe}-futures.json"


def summarize_ohlcv(path: Path) -> dict:
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not rows:
        return {"candles": 0, "start": None, "end": None}
    start = datetime.fromtimestamp(rows[0][0] / 1000, tz=timezone.utc).isoformat()
    end = datetime.fromtimestamp(rows[-1][0] / 1000, tz=timezone.utc).isoformat()
    return {"candles": len(rows), "start": start, "end": end}


def main() -> int:
    args = parse_args()
    futures_dir = args.dest_dir / "futures"
    futures_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_dir": str(args.source_dir),
        "dest_dir": str(args.dest_dir),
        "symbols": [symbol.upper() for symbol in args.symbols],
        "timeframes": args.timeframes,
        "files": [],
        "missing": [],
    }

    for symbol in args.symbols:
        for timeframe in args.timeframes:
            source = args.source_dir / source_name(symbol, args.source_quote, timeframe)
            target = futures_dir / target_name(symbol, args.target_quote, timeframe)
            if not source.exists():
                manifest["missing"].append(str(source))
                continue
            shutil.copy2(source, target)
            item = {
                "symbol": symbol.upper(),
                "timeframe": timeframe,
                "source": str(source),
                "target": str(target),
            }
            item.update(summarize_ohlcv(target))
            manifest["files"].append(item)
            print(
                f"{symbol.upper()} {timeframe}: {item['candles']} candles "
                f"{item['start']} -> {item['end']} | {target}"
            )

    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if manifest["missing"]:
        print("Missing source files:")
        for path in manifest["missing"]:
            print(f"  {path}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
