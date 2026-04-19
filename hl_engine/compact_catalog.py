"""
compact_catalog.py — Merge per-flush Parquet files into one file per calendar day.

For OrderBookDelta, injects a synthetic CLEAR + full snapshot at each day
boundary so backtests can start from any date without replaying from day 0.

All other types (Bar, TradeTick, FundingRate, OpenInterest) are compacted with
a simple concat + sort by ts_event, with no schema changes.

Output goes to data/catalog_compact. On --swap, the existing catalog is moved
to data/catalog_backup and the compact catalog takes its place.

Usage:
    uv run python3 compact_catalog.py                    # compact only
    uv run python3 compact_catalog.py --swap             # compact + swap in
    uv run python3 compact_catalog.py --dry-run          # show plan, no writes
    uv run python3 compact_catalog.py --catalog path/to/catalog
"""

import argparse
import logging
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.compute
import pyarrow.parquet as pq

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def day_bounds(d: date) -> tuple[datetime, datetime]:
    start = datetime(d.year, d.month, d.day, tzinfo=UTC)
    return start, start + timedelta(days=1)


def ts_to_date(ts_ns: int) -> date:
    return datetime.fromtimestamp(ts_ns / 1e9, tz=UTC).date()


def read_all(src_dir: Path) -> pa.Table | None:
    files = sorted(src_dir.glob("*.parquet"))
    if not files:
        return None
    log.info(f"    reading {len(files)} files …")
    return pa.concat_tables([pq.read_table(f) for f in files])


def write_day_files(table: pa.Table, out_dir: Path, dry_run: bool) -> int:
    """Split a sorted table by calendar day and write one file per day."""
    ts_col = table.column("ts_event").to_pylist()
    # Build list of (start_idx, end_idx, date) slices
    slices: list[tuple[int, int, date]] = []
    if not ts_col:
        return 0
    cur_date = ts_to_date(ts_col[0])
    cur_start = 0
    for i, ts in enumerate(ts_col):
        d = ts_to_date(ts)
        if d != cur_date:
            slices.append((cur_start, i, cur_date))
            cur_date = d
            cur_start = i
    slices.append((cur_start, len(ts_col), cur_date))

    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    for start, end, d in slices:
        day_table = table.slice(start, end - start)
        out_file = out_dir / f"{d.isoformat()}.parquet"
        if not dry_run:
            pq.write_table(day_table, out_file, compression="snappy")
        log.info(f"      {d}  {end-start:>8,} rows → {out_file.name}")

    return sum(e - s for s, e, _ in slices)


# ---------------------------------------------------------------------------
# Simple types: Bar, TradeTick, FundingRate, OpenInterest
# ---------------------------------------------------------------------------

def compact_simple(src_dir: Path, out_dir: Path, dry_run: bool) -> None:
    table = read_all(src_dir)
    if table is None:
        log.info(f"    {src_dir.name}: empty, skipping")
        return
    table = table.sort_by("ts_event")
    n = write_day_files(table, out_dir, dry_run)
    log.info(f"    → {n:,} total rows in {out_dir.name}")


# ---------------------------------------------------------------------------
# OrderBookDelta: snapshot-aware day compaction (pure pyarrow, no NT catalog)
# ---------------------------------------------------------------------------

# NT BookAction enum values as stored in parquet
_CLEAR_ACTION = 4
_ADD_ACTION   = 1
_DELETE_ACTION = 3
_ZERO_16 = b"\x00" * 16

def compact_ob_deltas(
    src_dir: Path,
    out_dir: Path,
    instrument,
    dry_run: bool,
) -> None:
    import re
    from collections import defaultdict

    files = sorted(src_dir.glob("*.parquet"))
    if not files:
        log.info("    no OB delta files, skipping")
        return

    log.info(f"    OB deltas: {len(files)} files, grouping by date …")

    # Group files by the date in their filename (ISO prefix YYYY-MM-DD).
    # Files are ~60 s of data so midnight-spanning files are rare; we handle
    # them by reading all files that START on a given date and then filtering
    # rows to the day's nanosecond bounds.
    day_files: dict[date, list[Path]] = defaultdict(list)
    for f in files:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", f.name)
        if m:
            day_files[date.fromisoformat(m.group(1))].append(f)
        else:
            # Fallback: read first ts from file to determine date
            t = pq.read_table(f, columns=["ts_event"]).column("ts_event")[0].as_py()
            day_files[ts_to_date(t)].append(f)

    # Read schema from first file for synthetic row construction
    schema = pq.read_schema(files[0])

    # book: (side_uint8, price_bytes) → size_bytes  (raw binary, no decoding)
    book: dict[tuple[int, bytes], bytes] = {}
    first_day = True

    for d in sorted(day_files.keys()):
        day_start, day_end = day_bounds(d)
        start_ns = int(day_start.timestamp() * 1e9)
        end_ns   = int(day_end.timestamp()   * 1e9)

        # Read and sort this day's files
        day_table = pa.concat_tables([pq.read_table(f) for f in day_files[d]])
        day_table = day_table.sort_by("ts_event")

        # Filter to exact day bounds (handles midnight-spanning files)
        ts_col = day_table.column("ts_event")
        mask = pa.compute.and_(
            pa.compute.greater_equal(ts_col, start_ns),
            pa.compute.less(ts_col, end_ns),
        )
        day_table = day_table.filter(mask)

        if day_table.num_rows == 0:
            log.info(f"      {d}  no rows after filter, skipping")
            first_day = False
            continue

        # Extract columns as Python lists for book-state replay
        action_col  = day_table.column("action").to_pylist()
        side_col    = day_table.column("side").to_pylist()
        price_col   = day_table.column("price").to_pylist()
        size_col    = day_table.column("size").to_pylist()

        # Build synthetic snapshot rows for day boundary (except first day)
        synth_table = None
        if not first_day and book:
            n_bids = sum(1 for (s, _) in book if s == 1)
            n_asks = sum(1 for (s, _) in book if s == 2)

            # One CLEAR row + one ADD per book level
            n_synth = 1 + len(book)
            synth = {
                "action":   [_CLEAR_ACTION] + [_ADD_ACTION] * len(book),
                "side":     [0] + [s for (s, _) in book],
                "price":    [_ZERO_16] + [p for (_, p) in book],
                "size":     [_ZERO_16] + [v for v in book.values()],
                "order_id": [0] * n_synth,
                "flags":    [0] * n_synth,
                "sequence": [0] * n_synth,
                "ts_event": [start_ns] * n_synth,
                "ts_init":  [start_ns] * n_synth,
            }
            synth_table = pa.table(synth, schema=schema)
            log.info(f"      {d}  injected snapshot: {n_bids} bids, {n_asks} asks")

        # Replay deltas to update book state
        for action, side, price, size in zip(action_col, side_col, price_col, size_col):
            if action == _CLEAR_ACTION:
                book.clear()
            elif action == _DELETE_ACTION or (isinstance(size, bytes) and size[:8] == b"\x00" * 8):
                book.pop((side, price), None)
            else:
                book[(side, price)] = size

        # Write compacted day file
        out_table = pa.concat_tables([synth_table, day_table]) if synth_table else day_table
        n_synth = synth_table.num_rows if synth_table else 0

        if not dry_run:
            out_dir.mkdir(parents=True, exist_ok=True)
            pq.write_table(out_table, out_dir / f"{d.isoformat()}.parquet", compression="snappy")

        log.info(
            f"      {d}  {day_table.num_rows:>7,} real + "
            f"{n_synth:>5,} synthetic = {out_table.num_rows:>7,} total"
        )
        first_day = False


# ---------------------------------------------------------------------------
# Instrument helper
# ---------------------------------------------------------------------------

def load_instrument(catalog_root: Path, coin: str):
    from nautilus_trader.persistence.catalog import ParquetDataCatalog
    cat = ParquetDataCatalog(str(catalog_root))
    instruments = cat.instruments()
    iid_str = f"{coin}-USD.HYPERLIQUID"
    for inst in instruments:
        if str(inst.id) == iid_str:
            return inst
    raise RuntimeError(f"Instrument {iid_str} not found in catalog")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Compact NautilusTrader Parquet catalog")
    parser.add_argument("--catalog", default="data/catalog", help="Source catalog path")
    parser.add_argument("--output", default="data/catalog_compact", help="Output catalog path")
    parser.add_argument("--coins", default="BTC", help="Comma-separated coins (default: BTC)")
    parser.add_argument("--swap", action="store_true", help="Swap compact catalog into place after compaction")
    parser.add_argument("--dry-run", action="store_true", help="Show plan without writing files")
    args = parser.parse_args()

    src = Path(args.catalog)
    out = Path(args.output)
    coins = [c.strip() for c in args.coins.split(",")]

    if args.dry_run:
        log.info("DRY RUN — no files will be written")

    if not args.dry_run and out.exists():
        log.warning(f"Output dir {out} already exists — removing")
        shutil.rmtree(out)

    for coin in coins:
        log.info(f"=== {coin} ===")
        venue = "HYPERLIQUID"
        iid = f"{coin}-USD.{venue}"

        # --- Bar ---
        log.info("  Bar (1m)")
        bar_type = f"{iid}-1-MINUTE-LAST-EXTERNAL"
        compact_simple(
            src / "data" / "bar" / bar_type,
            out / "data" / "bar" / bar_type,
            args.dry_run,
        )

        # --- TradeTick ---
        log.info("  TradeTick")
        compact_simple(
            src / "data" / "trade_tick" / iid,
            out / "data" / "trade_tick" / iid,
            args.dry_run,
        )

        # --- OrderBookDelta ---
        log.info("  OrderBookDelta")
        compact_ob_deltas(
            src / "data" / "order_book_deltas" / iid,
            out / "data" / "order_book_deltas" / iid,
            None,
            args.dry_run,
        )

        # --- Custom: FundingRate ---
        log.info("  FundingRate")
        compact_simple(
            src / "custom" / "funding_rate" / iid,
            out / "custom" / "funding_rate" / iid,
            args.dry_run,
        )

        # --- Custom: OpenInterest ---
        log.info("  OpenInterest")
        compact_simple(
            src / "custom" / "open_interest" / iid,
            out / "custom" / "open_interest" / iid,
            args.dry_run,
        )

    # Copy instrument metadata (tiny, just copy as-is)
    if not args.dry_run:
        for src_inst in (src / "data" / "crypto_perpetual").glob("**/*.parquet"):
            rel = src_inst.relative_to(src)
            dst = out / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_inst, dst)
        log.info("Copied instrument metadata.")

    log.info(f"Compaction complete → {out}")

    if args.swap and not args.dry_run:
        backup = src.parent / "catalog_backup"
        if backup.exists():
            shutil.rmtree(backup)
        src.rename(backup)
        out.rename(src)
        log.info(f"Swapped: {src} is now the compact catalog. Backup at {backup}.")
    elif args.swap and args.dry_run:
        log.info("(--swap skipped in dry-run mode)")


if __name__ == "__main__":
    main()
