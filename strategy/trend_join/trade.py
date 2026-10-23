import argparse
import csv
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from src.ibkr_client import IBKRClient  # noqa: E402

TRADES_CSV = ROOT / "trades.csv"
HEADER = ["timestamp_iso", "symbol", "side", "size", "fill_price", "order_id", "status"]
FAILED = {"Cancelled", "ApiCancelled", "Inactive"}


def append_row(row):
    for attempt in range(2):
        try:
            new = not TRADES_CSV.exists()
            with TRADES_CSV.open("a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(HEADER)
                w.writerow(row)
            return
        except (PermissionError, OSError) as e:
            if attempt == 0:
                time.sleep(1)
            else:
                print(f"trades.csv locked: {e}")
                sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--side", required=True, choices=["BUY", "SELL"])
    ap.add_argument("--size", required=True, type=int)
    a = ap.parse_args()

    load_dotenv(ROOT / ".env")
    try:
        ibkr = IBKRClient(os.getenv("IBKR_HOST", "127.0.0.1"), int(os.getenv("IBKR_PORT")),
                          int(os.getenv("IBKR_EXEC_CLIENT_ID")))
    except Exception as e:
        print(f"IBKR connection failed: {e!r}")
        sys.exit(1)

    try:
        trade = ibkr.place_order(a.symbol, a.side, a.size)
        try:  # wait up to 20s for the fill before logging
            from ib_insync import util
            for _ in range(80):
                if trade.orderStatus.status in ("Filled",) or trade.orderStatus.status in FAILED:
                    break
                util.sleep(0.25)
        except Exception:
            pass
        status = trade.orderStatus.status
        if status in FAILED:
            print(f"order {status}")
            for entry in trade.log:
                print(entry)
            sys.exit(2)
        fill = trade.orderStatus.avgFillPrice or 0
        append_row([datetime.now(timezone.utc).isoformat(), a.symbol, a.side, a.size,
                    fill, trade.order.orderId, status])
        print(f"order_id={trade.order.orderId} status={status} fill_price={fill or 'pending'}")
    finally:
        ibkr.disconnect()


if __name__ == "__main__":
    main()
