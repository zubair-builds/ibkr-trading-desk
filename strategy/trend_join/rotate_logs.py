"""Archive old logs, trim trades.csv to 90 days, cap safety-check-log.json at 5 MB."""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parent
LOGS = ROOT / "logs"
ARCHIVE = LOGS / "archive"
ET = ZoneInfo("America/New_York")


def main():
    today = datetime.now(ET).date()
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in LOGS.iterdir():
        if f.is_file() and f.suffix in (".log", ".jsonl"):
            d = datetime.fromtimestamp(f.stat().st_mtime, ET).date()
            if d < today:
                dest = ARCHIVE / str(d)
                dest.mkdir(exist_ok=True)
                os.replace(f, dest / f.name)
                n += 1

    csv = ROOT / "trades.csv"
    if csv.exists():
        df = pd.read_csv(csv)
        if not df.empty:
            ts = pd.to_datetime(df["timestamp_iso"], utc=True, errors="coerce").dt.tz_convert(ET)
            cutoff = pd.Timestamp(today - timedelta(days=90), tz=ET)
            old = ts < cutoff
            if old.any():
                arch = ARCHIVE / f"trades_{today:%Y%m%d}.csv"
                tmp_a, tmp_k = arch.with_suffix(".tmp"), csv.with_suffix(".tmp")
                df[old].to_csv(tmp_a, index=False, mode="w")
                if arch.exists():  # append to an earlier same-day archive
                    pd.concat([pd.read_csv(arch), df[old]]).to_csv(tmp_a, index=False)
                df[~old].to_csv(tmp_k, index=False)
                os.replace(tmp_a, arch)
                os.replace(tmp_k, csv)
                n += 1

    safety = ROOT / "safety-check-log.json"
    if safety.exists() and safety.stat().st_size > 5 * 1024 * 1024:
        os.replace(safety, ARCHIVE / f"{today}_safety-check-log.json")
        n += 1

    print(f"Rotated {n} files to logs/archive/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
