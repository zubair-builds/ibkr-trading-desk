"""Daily performance summary from trades.csv (read-only) + ntfy alert."""
import json
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
from src.notify import notify  # noqa: E402

ET = ZoneInfo("America/New_York")


def main():
    today = datetime.now(ET).date()
    csv = ROOT / "trades.csv"
    df = pd.read_csv(csv) if csv.exists() else pd.DataFrame()
    pairs = []
    if not df.empty:
        df["ts"] = pd.to_datetime(df["timestamp_iso"], utc=True, errors="coerce").dt.tz_convert(ET)
        df = df[(df["ts"].dt.date <= today) & (df["status"].astype(str).str.lower() == "filled")]
        df = df.sort_values("ts")
        lots = defaultdict(deque)
        for _, r in df.iterrows():
            sym, side = r["symbol"], str(r["side"]).upper()
            qty, px = float(r["size"]), float(r["fill_price"])
            if side == "BUY":
                lots[sym].append([qty, px, r["ts"]])
                continue
            while qty > 0 and lots[sym]:
                lot = lots[sym][0]
                q = min(qty, lot[0])
                pnl = (px - lot[1]) * q
                if r["ts"].date() == today:
                    pairs.append({"symbol": sym, "pnl": pnl, "pnl_pct": (px / lot[1] - 1) * 100,
                                  "hold_min": (r["ts"] - lot[2]).total_seconds() / 60})
                lot[0] -= q
                qty -= q
                if lot[0] <= 0:
                    lots[sym].popleft()
    wins = [p for p in pairs if p["pnl"] > 0]
    losses = [p for p in pairs if p["pnl"] <= 0]
    sw, sl = sum(p["pnl"] for p in wins), sum(p["pnl"] for p in losses)
    n = len(pairs)
    best = max(pairs, key=lambda p: p["pnl"], default=None)
    worst = min(pairs, key=lambda p: p["pnl"], default=None)
    pf = "n/a" if n == 0 else ("inf" if sl == 0 else round(sw / abs(sl), 2))
    summary = {
        "date": str(today), "total_trades": n, "wins": len(wins), "losses": len(losses),
        "win_rate_pct": round(len(wins) / n * 100, 1) if n else 0.0,
        "gross_pnl_usd": round(sw + sl, 2),
        "largest_winner": best and {"symbol": best["symbol"], "pnl": round(best["pnl"], 2)},
        "largest_loser": worst and {"symbol": worst["symbol"], "pnl": round(worst["pnl"], 2)},
        "avg_winner": round(sw / len(wins), 2) if wins else 0.0,
        "avg_loser": round(sl / len(losses), 2) if losses else 0.0,
        "profit_factor": pf,
        "trades": [{k: (round(v, 2) if isinstance(v, float) else v) for k, v in p.items()} for p in pairs],
    }
    print(json.dumps(summary, indent=2))
    if n == 0:
        body = "No closed trades today."
    else:
        body = (f"Trades: {n} ({len(wins)}W / {len(losses)}L, {summary['win_rate_pct']}%)\n"
                f"P&L: ${sw + sl:+.2f}\nBest: {best['symbol']} ${best['pnl']:+.2f}\n"
                f"Worst: {worst['symbol']} ${worst['pnl']:+.2f}\nPF: {pf}")
    try:
        notify(f"Daily Summary {today}", body, "default", "bar_chart")
    except Exception:
        pass


if __name__ == "__main__":
    main()
