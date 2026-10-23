import json
import math
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ib_insync import Stock

ROOT = Path(__file__).resolve().parent
RULES_PATH = ROOT / "rules.json"
LOG_DIR = ROOT / "logs"
SAFETY_LOG = LOG_DIR / "safety_log.jsonl"
ET = ZoneInfo("America/New_York")


def _log(result: dict) -> dict:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with SAFETY_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(result) + "\n")
    return result


def _result(passed, reasons, price, symbol):
    return _log({
        "timestamp_et": datetime.now(ET).isoformat(),
        "symbol": symbol,
        "pass": bool(passed),
        "reasons": list(reasons),
        "price": float(price),
    })


def _snapshot_price(symbol: str, ib) -> float:
    contract = Stock(symbol, "SMART", "USD")
    ib.qualifyContracts(contract)
    ticker = ib.reqMktData(contract, "", True, False)
    for _ in range(20):
        ib.sleep(0.25)
        p = ticker.marketPrice()
        if p is not None and not math.isnan(p) and p > 0:
            return float(p)
    for p in (ticker.last, ticker.close):
        if p is not None and not math.isnan(p) and p > 0:
            return float(p)
    return 0.0


def evaluate(symbol: str, ib) -> dict:
    # a) already in position
    for pos in ib.positions():
        if pos.contract.symbol == symbol and pos.position > 0:
            return _result(False, ["already in position"], 0.0, symbol)

    # b) time window
    rules = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    tf = rules["time_filter"]
    earliest, latest = tf["earliest_entry_et"], tf["latest_entry_et"]
    now_hm = datetime.now(ET).strftime("%H:%M")
    if not (earliest <= now_hm <= latest):
        return _result(False, [f"outside entry window {earliest}-{latest}"], 0.0, symbol)

    # c) price
    price = _snapshot_price(symbol, ib)
    return _result(True, ["time gate ok", "no existing position"], price, symbol)
