"""Autonomous trading cycle for the Trend Join Long strategy (paper trading).

Run every 5 minutes during the US session. Outside market hours it exits in
well under a second because heavy imports happen only after the time gate.
"""
import sys
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent


# ---------------------------------------------------------------- 1. time gate
def time_gate(now=None) -> str:
    now = now or datetime.now(ET)
    if now.weekday() >= 5:
        return "weekend"
    t = now.time()
    if t < dtime(10, 0):
        return "too_early"
    if t >= dtime(16, 0):
        return "closed"
    if t >= dtime(15, 51):
        return "force_close"
    if t < dtime(10, 5) or t >= dtime(15, 30):
        return "manage_only"
    return "ok"


if __name__ == "__main__":
    _status = time_gate()
    if _status in {"weekend", "too_early", "closed"}:
        print(f"[{datetime.now(ET):%H:%M:%S} ET] {_status}: exiting")
        sys.exit(0)

# Heavy imports only past the gate.
import csv  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402
from datetime import timedelta, timezone  # noqa: E402

from dotenv import load_dotenv  # noqa: E402

sys.path.insert(0, str(ROOT))

STATE_PATH = ROOT / "open_positions.json"
TRADES_CSV = ROOT / "trades.csv"
WATCHLIST = ROOT / "watchlist.txt"
RULES_PATH = ROOT / "rules.json"
SAFETY_LOG = ROOT / "safety-check-log.json"
LOG_DIR = ROOT / "logs"
ERROR_LOG = LOG_DIR / "cycle_errors.log"
SUBPROCESS_TIMEOUT = 30


def say(msg):
    print(f"[{datetime.now(ET):%H:%M:%S} ET] {msg}", flush=True)


def _clean(v):
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    if type(v).__name__ == "bool_" or isinstance(v, bool):
        return bool(v)
    if hasattr(v, "item") and not isinstance(v, (str, bytes)):
        try:
            return v.item()
        except Exception:
            return str(v)
    return v


def log_event(event: str, **fields):
    rec = {"ts_et": datetime.now(ET).isoformat(), "event": event, **fields}
    with SAFETY_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(_clean(rec), default=str) + "\n")
    say(f"{event}: {json.dumps(_clean(fields), default=str)}")
    try:
        from src.notify import notify_event
        notify_event(event, _clean(fields))
    except Exception:
        pass


# ---------------------------------------------------------------- 2/5. state
def load_state() -> list:
    if not STATE_PATH.exists():
        return []
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8")) or []
    except json.JSONDecodeError:
        log_event("state_corrupt", path=str(STATE_PATH))
        return []


def save_state(positions: list):
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(_clean(positions), indent=2, default=str), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


# ---------------------------------------------------------------- IBKR helpers
def connect(host, port, client_id):
    from ib_insync import IB
    for attempt in range(2):
        ib = IB()
        try:
            ib.connect(host, port, clientId=client_id, timeout=10)
            return ib
        except Exception as e:
            say(f"IBKR connect failed (attempt {attempt + 1}): {e!r}")
            if attempt == 0:
                time.sleep(5)
    sys.exit(1)


def stock(ib, symbol):
    from ib_insync import Stock
    c = Stock(symbol, "SMART", "USD")
    q = ib.qualifyContracts(c)
    if not q:
        raise ValueError(f"cannot qualify {symbol}")
    return q[0]


def ib_price(ib, symbol) -> float:
    try:
        ib.reqMarketDataType(3)  # live if subscribed, else free delayed
    except Exception:
        pass
    t = ib.reqMktData(stock(ib, symbol), "", True, False)
    for _ in range(20):
        ib.sleep(0.25)
        p = t.marketPrice()
        if p and not math.isnan(p) and p > 0:
            return float(p)
    for p in (t.last, t.close):
        if p and not math.isnan(p) and p > 0:
            return float(p)
    try:  # fallback: Yahoo 1-minute last price
        import yfinance as yf
        df = yf.download(symbol.replace(" ", "-"), period="1d", interval="1m", progress=False, prepost=False)
        if df is not None and not df.empty:
            v = float(df["Close"].dropna().iloc[-1].squeeze())
            if v > 0:
                return v
    except Exception:
        pass
    return 0.0


def place_stop(ib, symbol, qty, stop_price):
    from ib_insync import StopOrder
    o = StopOrder("SELL", int(qty), round(float(stop_price), 2), tif="DAY")
    trade = ib.placeOrder(stock(ib, symbol), o)
    ib.sleep(1)
    return trade.order.orderId


def cancel_order(ib, order_id):
    if not order_id:
        return
    for t in ib.openTrades():
        if t.order.orderId == order_id:
            ib.cancelOrder(t.order)
            ib.sleep(1)
            return


def run_trade(symbol, side, size) -> bool:
    cmd = [sys.executable, str(ROOT / "trade.py"), "--symbol", symbol, "--side", side, "--size", str(int(size))]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT, cwd=ROOT)
    except subprocess.TimeoutExpired:
        log_event("trade_timeout", symbol=symbol, side=side, size=size)
        return False
    for line in (p.stdout or "").splitlines():
        say(f"trade.py: {line}")
    if p.returncode != 0:
        log_event("trade_failed", symbol=symbol, side=side, size=size, rc=p.returncode,
                  stderr=(p.stderr or "")[-500:])
        return False
    return True


def last_fill_price(symbol, side) -> float:
    if not TRADES_CSV.exists():
        return 0.0
    with TRADES_CSV.open(newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r["symbol"] == symbol and r["side"] == side]
    try:
        return float(rows[-1]["fill_price"]) if rows else 0.0
    except ValueError:
        return 0.0


# ---------------------------------------------------------------- 3. stop-outs
def check_stop_outs(ib, positions):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    filled_ids = set()
    for f in ib.reqExecutions():
        ts = f.execution.time
        if ts and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts and ts >= cutoff:
            filled_ids.add(f.execution.orderId)
    kept = []
    for p in positions:
        if p.get("stop_order_id") and p["stop_order_id"] in filled_ids:  # match by order id ONLY
            log_event("stopped_out", symbol=p["symbol"], stop_order_id=p["stop_order_id"],
                      stop_price=p.get("stop_price"))
        else:
            kept.append(p)
    return kept


# ---------------------------------------------------------------- 4. manage
def swing_lows(df):
    lows = df["Low"].tolist()
    out = []
    for i in range(2, len(lows) - 2):
        if lows[i] < lows[i - 1] and lows[i] < lows[i - 2] and lows[i] < lows[i + 1] and lows[i] < lows[i + 2]:
            out.append(float(lows[i]))
    return out


def bars_5m_today(symbol):
    import yfinance as yf
    df = yf.download(symbol.replace(" ", "-"), period="1d", interval="5m", progress=False,
                     auto_adjust=True, prepost=False)
    if df is None or df.empty:
        return None
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = df.columns.get_level_values(0)
    return df.dropna(how="all")


def manage(ib, p):
    sym = p["symbol"]
    price = ib_price(ib, sym)
    if price <= 0:
        log_event("manage_no_price", symbol=sym)
        return p
    entry, R, qty = float(p["entry_price"]), float(p["R"]), int(p["qty"])

    if p["state"] == "pre_breakeven":
        if price >= entry + 1.0 * R:
            cancel_order(ib, p["stop_order_id"])
            p["stop_order_id"] = place_stop(ib, sym, qty, entry)
            p["stop_price"] = round(entry, 2)
            p["state"] = "post_breakeven_no_partial"
            log_event("breakeven_stop", symbol=sym, price=price, new_stop=p["stop_price"])
        elif price >= entry + 0.75 * R:
            sell_qty = math.ceil(qty / 3)
            remaining = qty - sell_qty
            cancel_order(ib, p["stop_order_id"])
            if run_trade(sym, "SELL", sell_qty):
                p["qty"] = remaining
                log_event("partial_profit", symbol=sym, sold=sell_qty, price=price, remaining=remaining)
            if p["qty"] >= 1:
                new_stop = round(entry * 0.99, 2)
                p["stop_order_id"] = place_stop(ib, sym, p["qty"], new_stop)
                p["stop_price"] = new_stop
                p["state"] = "post_breakeven_partial_done"
            else:
                p["state"] = "closed"
        return p

    if p["state"].startswith("post_breakeven"):
        df = bars_5m_today(sym)
        lows = swing_lows(df) if df is not None else []
        if lows:
            newest = lows[-1]
            candidate = round(newest - 0.01, 2)
            current = float(p.get("stop_price") or 0)
            if candidate > current and candidate < price:  # ratchet up only
                cancel_order(ib, p["stop_order_id"])
                p["stop_order_id"] = place_stop(ib, sym, p["qty"], candidate)
                p["stop_price"] = candidate
                log_event("trail_stop", symbol=sym, old_stop=current, new_stop=candidate, price=price)
    return p


# ---------------------------------------------------------------- 6. force close
def force_close(ib, positions):
    for p in positions:
        cancel_order(ib, p.get("stop_order_id"))
        ok = run_trade(p["symbol"], "SELL", p["qty"]) if int(p["qty"]) >= 1 else True
        log_event("force_close", symbol=p["symbol"], qty=p["qty"], success=ok)
    save_state([])


# ---------------------------------------------------------------- 8. entry scan
def today_buy_count() -> int:
    if not TRADES_CSV.exists():
        return 0
    today = datetime.now(ET).date()
    n = 0
    with TRADES_CSV.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("side") != "BUY":
                continue
            try:
                ts = datetime.fromisoformat(r["timestamp_iso"])
                ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
                if ts.astimezone(ET).date() == today:
                    n += 1
            except (KeyError, ValueError):
                pass
    return n


def read_watchlist():
    out = []
    for line in WATCHLIST.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line.split("#")[0].strip())
    return [t for t in out if t]


def evaluate_filters(symbol, rules):
    """Run D1-D3 and I1-I3. Returns (passed, reasons, ctx)."""
    import yfinance as yf
    ysym = symbol.replace(" ", "-")
    daily = yf.download(ysym, period="1y", interval="1d", progress=False, auto_adjust=True)
    intra = yf.download(ysym, period="20d", interval="5m", progress=False, auto_adjust=True, prepost=True)
    for df in (daily, intra):
        if df is not None and not df.empty and df.columns.nlevels > 1:
            df.columns = df.columns.get_level_values(0)
    if daily is None or len(daily) < 201 or intra is None or intra.empty:
        return False, ["insufficient data"], {}

    intra = intra.dropna(how="all")
    intra.index = intra.index.tz_convert(ET)
    today = datetime.now(ET).date()
    today_bars = intra[intra.index.date == today]
    if today_bars.empty:
        return False, ["no intraday bars today"], {}
    rth = today_bars.between_time("09:30", "15:59")
    pre = today_bars.between_time("04:00", "09:29")
    if rth.empty:
        return False, ["no regular-session bars yet"], {}

    price = float(rth["Close"].iloc[-1])
    prior = daily[daily.index.date < today]
    prior_high = float(prior["High"].iloc[-1])
    prior_close = float(prior["Close"].iloc[-1])
    sma200 = float(prior["Close"].iloc[-200:].mean())
    gap_pct = (price - prior_close) / prior_close * 100
    hod_before = float(rth["High"].iloc[:-1].max()) if len(rth) > 1 else float(rth["High"].iloc[-1])
    lod = float(rth["Low"].min())
    pm_high = float(pre["High"].max()) if not pre.empty else None

    # RVOL: today's cumulative RTH volume vs avg cumulative volume at same time over lookback days
    df_f, if_ = rules["daily_filters"], rules["intraday_filters"]
    lookback = int(if_["I3_rvol_lookback_days"])
    cutoff_t = rth.index[-1].time()
    cum_today = float(rth["Volume"].sum())
    hist = []
    all_rth = intra.between_time("09:30", "15:59")
    for d in sorted({x for x in all_rth.index.date if x < today})[-lookback:]:
        day = all_rth[all_rth.index.date == d]
        hist.append(float(day[day.index.time <= cutoff_t]["Volume"].sum()))
    avg_hist = sum(hist) / len(hist) if hist else 0
    rvol = cum_today / avg_hist if avg_hist > 0 else 0.0

    checks = {
        "D1_above_prior_day_high": (not df_f["D1_above_prior_day_high"]) or price > prior_high,
        "D2_prior_close_above_sma200": (not df_f["D2_prior_close_above_sma200"]) or prior_close > sma200,
        "D3_min_gap_pct": gap_pct >= float(df_f["D3_min_gap_pct_from_prior_close"]),
        "I1_above_premarket_high": (not if_["I1_above_premarket_high"]) or (pm_high is not None and price > pm_high),
        "I2_above_today_hod": (not if_["I2_above_today_hod"]) or price > hod_before,
        "I3_rvol": rvol >= float(if_["I3_rvol_min"]),
        "min_price": price >= float(rules["universe_filters"]["min_price_usd"]),
    }
    checks = {k: bool(v) for k, v in checks.items()}
    ctx = {"price": price, "prior_high": prior_high, "prior_close": prior_close, "sma200": round(sma200, 2),
           "gap_pct": round(gap_pct, 2), "hod_before": hod_before, "pm_high": pm_high,
           "lod": lod, "rvol": round(rvol, 2), "checks": checks}
    failed = [k for k, v in checks.items() if not v]
    return (not failed), (failed or ["all filters passed"]), ctx


def entry_scan(ib, positions, env):
    max_trades = int(env["MAX_TRADES_PER_DAY"])
    if today_buy_count() >= max_trades:
        log_event("entry_skip_daily_cap", count=today_buy_count(), max=max_trades)
        return positions

    rules = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    max_conc = int(rules["risk"]["max_concurrent_positions"])
    held = {p.contract.symbol for p in ib.positions() if p.position != 0}  # FIRST: broker truth
    held |= {p["symbol"] for p in positions}
    portfolio = env["PORTFOLIO_VALUE_USD"]
    risk_dollars = portfolio * (env["MAX_RISK_PER_TRADE_PCT"] / 100)
    max_pos_pct = float(rules["risk"]["max_position_size_pct_of_portfolio"]) / 100

    for sym in read_watchlist():
        if len(positions) >= max_conc:
            log_event("entry_skip_max_concurrent", max=max_conc)
            break
        if today_buy_count() >= max_trades:
            log_event("entry_skip_daily_cap", max=max_trades)
            break
        if sym in held:
            log_event("entry_skip_held", symbol=sym)
            continue
        try:
            passed, reasons, ctx = evaluate_filters(sym, rules)
        except Exception as e:
            log_event("entry_filter_error", symbol=sym, error=repr(e))
            continue
        if not passed:
            log_event("entry_skip_filters", symbol=sym, failed=reasons, ctx=ctx)
            continue

        price = ctx["price"]
        initial_stop = round(ctx["lod"] * 0.99, 2)
        R = price - initial_stop
        if R <= 0:
            log_event("entry_skip_bad_R", symbol=sym, price=price, stop=initial_stop)
            continue
        size = min(math.floor(risk_dollars / R), math.floor(portfolio * max_pos_pct / price),
                   math.floor(env["MAX_TRADE_SIZE_USD"] / price))
        if size < 1:
            log_event("entry_skip_size", symbol=sym, price=price, R=R)
            continue

        log_event("entry_signal", symbol=sym, size=size, price=price, stop=initial_stop, R=round(R, 2), ctx=ctx)
        if not run_trade(sym, "BUY", size):
            continue
        fill = last_fill_price(sym, "BUY") or price
        stop_id = place_stop(ib, sym, size, initial_stop)
        positions.append({
            "symbol": sym, "entry_price": fill, "entry_time_iso": datetime.now(timezone.utc).isoformat(),
            "qty": size, "initial_stop": initial_stop, "stop_price": initial_stop,
            "stop_order_id": stop_id, "state": "pre_breakeven", "R": round(fill - initial_stop, 4),
        })
        held.add(sym)
        save_state(positions)
        log_event("entered", symbol=sym, qty=size, fill=fill, stop=initial_stop, stop_order_id=stop_id)
    return positions


# ---------------------------------------------------------------- main
def load_env():
    load_dotenv(ROOT / ".env")
    paper = os.getenv("PAPER_TRADING", "true").strip().lower() == "true"
    port = int(os.getenv("IBKR_PORT"))
    if paper and port in {7496, 4001, 4003}:
        sys.exit("ABORT: paper flag but live port")
    if not paper and port in {7497, 4002, 4004}:
        sys.exit("ABORT: live flag but paper port")
    pv = os.getenv("PORTFOLIO_VALUE_USD")
    if not pv:
        sys.exit("ABORT: PORTFOLIO_VALUE_USD is not set in .env")
    return {
        "host": os.getenv("IBKR_HOST", "127.0.0.1"), "port": port,
        "client_id": int(os.getenv("IBKR_CLIENT_ID")),
        "PORTFOLIO_VALUE_USD": float(pv),
        "MAX_TRADE_SIZE_USD": float(os.getenv("MAX_TRADE_SIZE_USD", "2500")),
        "MAX_TRADES_PER_DAY": int(os.getenv("MAX_TRADES_PER_DAY", "5")),
        "MAX_RISK_PER_TRADE_PCT": float(os.getenv("MAX_RISK_PER_TRADE_PCT", "1.0")),
    }


def main(status: str) -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    env = load_env()
    if status == "manage_only" or status == "ok":
        if not WATCHLIST.exists() and status == "ok":
            log_event("no_watchlist", path=str(WATCHLIST))
    ib = connect(env["host"], env["port"], env["client_id"])
    try:
        positions = load_state()                                   # 2
        positions = check_stop_outs(ib, positions)                 # 3
        positions = [manage(ib, p) for p in positions]             # 4
        positions = [p for p in positions if p.get("state") != "closed"]
        save_state(positions)                                      # 5
        if status == "force_close":                                # 6
            force_close(ib, positions)
            return 0
        if status == "manage_only":                                # 7
            say("manage_only: no new entries")
            return 0
        if WATCHLIST.exists():                                     # 8
            positions = entry_scan(ib, positions, env)
        save_state(positions)                                      # 9
        return 0
    finally:
        ib.disconnect()


if __name__ == "__main__":
    try:
        sys.exit(main(_status))
    except SystemExit:
        raise
    except Exception:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with ERROR_LOG.open("a", encoding="utf-8") as f:
            f.write(f"--- {datetime.now(ET).isoformat()} ---\n{traceback.format_exc()}\n")
        say("unhandled error, see logs/cycle_errors.log")
        try:
            from src.notify import notify
            notify("cycle.py CRASHED", traceback.format_exc()[-800:], "urgent", "rotating_light")
        except Exception:
            pass
        sys.exit(1)
