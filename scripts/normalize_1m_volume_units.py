"""Normalize pre-2026-05-26 1m volume units from shares to lots.

Before 2026-05-26, volume was recorded in shares. After that date, volume is in lots
(1 lot = 100 shares). This script divides volumes on rows dated <= 20260525 by 100.

Default mode is dry-run. Use --apply to rewrite files.

Examples:
    python scripts/normalize_1m_volume_units.py
    python scripts/normalize_1m_volume_units.py --apply
"""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path


DATE_COL = "日期"
VOLUME_COL = "成交量(股)"
AMOUNT_COL = "成交额(元)"
CLOSE_COL = "收盘"


def _compact_date(value: str) -> str:
    value = str(value).strip()
    if len(value) >= 10 and value[4] == "-" and value[7] == "-":
        return value[:10].replace("-", "")
    return value[:8].replace("-", "")


def _parse_float(value: str) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _format_volume(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _detect_encoding(path: Path) -> str:
    with path.open("rb") as f:
        prefix = f.read(3)
    return "utf-8-sig" if prefix == b"\xef\xbb\xbf" else "utf-8"


def _column_indexes(header: list[str]) -> tuple[int, int, int, int]:
    missing = [name for name in (DATE_COL, VOLUME_COL, AMOUNT_COL, CLOSE_COL) if name not in header]
    if missing:
        raise ValueError(f"missing required columns: {missing}; header={header}")
    return header.index(DATE_COL), header.index(VOLUME_COL), header.index(AMOUNT_COL), header.index(CLOSE_COL)


def count_convertible_rows(path: Path, cutoff_key: str) -> tuple[int, float]:
    encoding = _detect_encoding(path)
    count = 0
    raw_volume_sum = 0.0

    with path.open("r", encoding=encoding, newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            return 0, 0.0
        date_idx, volume_idx, _, _ = _column_indexes(header)

        for row in reader:
            if len(row) <= max(date_idx, volume_idx):
                continue
            date_key = _compact_date(row[date_idx])
            if not date_key or date_key > cutoff_key:
                continue
            volume = _parse_float(row[volume_idx])
            if volume is None:
                continue
            count += 1
            raw_volume_sum += volume

    return count, raw_volume_sum


def rewrite_file(path: Path, cutoff_key: str, backup_dir: Path) -> int:
    encoding = _detect_encoding(path)
    tmp_path = path.with_name(path.name + ".tmp_normalize_volume")
    converted_rows = 0

    backup_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup_dir / path.name)

    try:
        with path.open("r", encoding=encoding, newline="") as src, tmp_path.open(
            "w", encoding=encoding, newline=""
        ) as dst:
            reader = csv.reader(src)
            writer = csv.writer(dst, lineterminator="\n")
            header = next(reader, None)
            if not header:
                return 0
            date_idx, volume_idx, _, _ = _column_indexes(header)
            writer.writerow(header)

            for row in reader:
                if len(row) > max(date_idx, volume_idx):
                    date_key = _compact_date(row[date_idx])
                    if date_key and date_key <= cutoff_key:
                        volume = _parse_float(row[volume_idx])
                        if volume is not None:
                            row[volume_idx] = _format_volume(volume / 100.0)
                            converted_rows += 1
                writer.writerow(row)

        os.replace(tmp_path, path)
        return converted_rows
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def rebuild_indexes(klines_root: Path, workers: int) -> None:
    print(f"[index] rebuilding 1m indexes under {klines_root} with workers={workers} ...", flush=True)
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from src.market.data.indexer import build_all_1m_indexes

    count = build_all_1m_indexes(klines_root, workers=workers)
    print(f"[index] rebuilt indexes for {count} files", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="C:/klines/1m", help="Directory containing per-ticker 1m CSV files.")
    parser.add_argument(
        "--cutoff",
        default="20260525",
        help="Convert rows with date <= cutoff. Accepts YYYYMMDD or YYYY-MM-DD. Default is 20260525.",
    )
    parser.add_argument("--apply", action="store_true", help="Actually rewrite files. Default is dry-run.")
    parser.add_argument("--backup-dir", default="", help="Backup directory. Default is timestamped beside 1m dir.")
    parser.add_argument("--progress-every", type=int, default=100, help="Print progress every N files.")
    parser.add_argument("--no-rebuild-index", action="store_true", help="Skip rebuilding 1m indexes after --apply.")
    parser.add_argument("--index-workers", type=int, default=16, help="Workers for index rebuild.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = Path(args.root)
    cutoff_key = _compact_date(args.cutoff)
    files = sorted(root.glob("*.csv"))
    if not files:
        raise SystemExit(f"No CSV files found under {root}")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = Path(args.backup_dir) if args.backup_dir else root.parent / f"1m_backup_before_volume_normalize_{timestamp}"

    print(
        f"[start] root={root} files={len(files)} cutoff<={cutoff_key} "
        f"mode={'apply' if args.apply else 'dry-run'}",
        flush=True,
    )
    print("[rule] all rows with date <= cutoff: volume = raw_volume / 100", flush=True)

    to_convert: list[tuple[Path, int, float]] = []
    errors: list[tuple[str, str]] = []
    for idx, path in enumerate(files, 1):
        try:
            count, vol_sum = count_convertible_rows(path, cutoff_key)
            if count > 0:
                to_convert.append((path, count, vol_sum))
        except Exception as exc:
            errors.append((path.name, f"{type(exc).__name__}: {exc}"))

        if idx == 1 or idx % args.progress_every == 0 or idx == len(files):
            planned_rows = sum(r for _, r, _ in to_convert)
            print(
                f"[scan] {idx}/{len(files)} files, planned_files={len(to_convert)}, "
                f"planned_rows={planned_rows}, errors={len(errors)}",
                flush=True,
            )

    planned_rows = sum(r for _, r, _ in to_convert)
    before = sum(v for _, _, v in to_convert)
    after = before / 100.0
    print(
        f"[plan] files_to_rewrite={len(to_convert)} rows_to_convert={planned_rows} "
        f"raw_volume_before={before:.2f} raw_volume_after={after:.2f}",
        flush=True,
    )

    if to_convert:
        print("[plan] sample files:", flush=True)
        for path, rows, vol in to_convert[:20]:
            print(
                f"  {path.name}: rows={rows} volume {vol:.2f}->{vol/100:.2f}",
                flush=True,
            )

    if errors:
        print("[warn] scan errors:", flush=True)
        for name, message in errors[:20]:
            print(f"  {name}: {message}", flush=True)
        if len(errors) > 20:
            print(f"  ... {len(errors) - 20} more", flush=True)

    if not args.apply:
        print("[done] dry-run only. Re-run with --apply to rewrite files.", flush=True)
        return

    print(f"[write] backup_dir={backup_dir}", flush=True)
    written_files = 0
    written_rows = 0
    for idx, (path, _, _) in enumerate(to_convert, 1):
        rows = rewrite_file(path, cutoff_key, backup_dir)
        written_files += 1
        written_rows += rows
        if idx == 1 or idx % args.progress_every == 0 or idx == len(to_convert):
            print(
                f"[write] {idx}/{len(to_convert)} files, converted_rows={written_rows}",
                flush=True,
            )

    print(f"[write] complete files={written_files} converted_rows={written_rows}", flush=True)

    if args.no_rebuild_index:
        print("[index] skipped. WARNING: existing 1m index offsets may now be stale.", flush=True)
    else:
        rebuild_indexes(root.parent, args.index_workers)

    print("[done] volume units normalized to lot-based 1m volume.", flush=True)


if __name__ == "__main__":
    main()
