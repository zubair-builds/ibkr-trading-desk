import argparse
import csv
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from src.ibkr_client import IBKRClient  # noqa: E402
import strategy  # noqa: E402

ET = ZoneInfo("America/New_York")
TRADES_CSV = ROOT / "trades.csv"
HEADER = ["timestamp_iso", "symbol", "side", "size", "fill_price", "order_id", "status"]
LIVE_PORTS = {7496, 4001, 4003}
PAPER_PORTS = {7497, 4002, 4004}


def log(msg):
    print(f"[{datetime.now(ET).strftime('%H:%M:%S')} ET] {msg}", flush=True)


def read_rows():
    for attempt in range(2):
        try:
            if not TRADES_CSV.exists():
                with TRADES_CSV.open("w", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(HEADER)
            with TRADES_CSV.open(newline="", encoding="utf-8") as f:
                return list(csv.DictReader(f))
        except (PermissionError, OSError) as e:
            if attempt == 0:
                time.sleep(1)
            else:
                log(f"trades.csv locked: {e}")
                sys.exit(1)


def today_buys(rows):
    today = datetime.now(ET).date()
    n = 0
    for r in rows:
        if r.get("side") != "BUY":
            continue
        try:
            ts = datetime.fromisoformat(r["timestamp_iso"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts.astimezone(ET).date() == today:
                n += 1
        except (KeyError, ValueError):
            continue
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--check-only", action="store_true")
    a = ap.parse_args()
    symbol = a.symbol.upper()

    # 1. env
    load_dotenv(ROOT / ".env")
    paper = os.getenv("PAPER_TRADING", "true").strip().lower() == "true"
    port = int(os.getenv("IBKR_PORT"))

    # 2. paper/live guard
    if paper and port in LIVE_PORTS:
        sys.exit("ABORT: paper flag but live port")
    if not paper and port in PAPER_PORTS:
        sys.exit("ABORT: live flag but paper port")

    # 3. daily trade cap
    max_trades = int(os.getenv("MAX_TRADES_PER_DAY", "5"))
    count = today_buys(read_rows())
    if count >= max_trades:
        log(f"daily trade cap reached ({count}/{max_trades}), exiting")
        sys.exit(0)

    # 4. connect
    try:
        ibkr = IBKRClient(os.getenv("IBKR_HOST", "127.0.0.1"), port, int(os.getenv("IBKR_CLIENT_ID")))
    except Exception as e:
        log(f"IBKR connection failed: {e!r}")
        sys.exit(1)

    try:
        # 5. evaluate
        result = strategy.evaluate(symbol, ibkr.ib)
        log(f"result: {result}")

        # 6/7
        if a.check_only:
            return
        if not result["pass"]:
            log(f"not taking trade: {', '.join(result['reasons'])}")
            return

        # 8. sizing
        price = result["price"]
        portfolio = float(os.getenv("PORTFOLIO_VALUE_USD") or 0)
        max_trade = float(os.getenv("MAX_TRADE_SIZE_USD", "2500"))
        budget = min(max_trade, portfolio * 0.10) if portfolio > 0 else max_trade
        if price <= 0:
            log("no valid price, skipping")
            return
        qty = int(budget / price)
        if qty < 1:
            log("position too small")
            return
        log(f"sizing: budget=${budget:.2f} price=${price:.2f} qty={qty}")
    finally:
        ibkr.disconnect()

    # 9. execute in separate process
    cmd = [sys.executable, str(ROOT / "trade.py"), "--symbol", symbol, "--side", "BUY", "--size", str(qty)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, cwd=ROOT)
    except subprocess.TimeoutExpired:
        log("trade.py timed out after 30s (killed)")
        sys.exit(1)
    for line in (proc.stdout or "").splitlines():
        log(f"trade.py: {line}")
    for line in (proc.stderr or "").splitlines():
        log(f"trade.py stderr: {line}")

    # 10. verify
    rows = read_rows()
    last = rows[-1] if rows else None
    if proc.returncode == 0 and last and last["symbol"] == symbol and last["status"] not in {"Cancelled", "ApiCancelled", "Inactive"}:
        log(f"SUCCESS: {last['side']} {last['size']} {symbol} status={last['status']} fill={last['fill_price']}")
    else:
        log(f"FAILURE: trade.py exit={proc.returncode}, last row={last}")
        sys.exit(1)


if __name__ == "__main__":
    main()
