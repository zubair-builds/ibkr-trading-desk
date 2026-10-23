"""Push notifications via ntfy.sh. Never raises: alerts must not break trading."""
import os

import requests

ALERT_EVENTS = {
    "entered": ("BUY", "default", "chart_with_upwards_trend"),
    "partial_profit": ("PARTIAL", "default", "moneybag"),
    "breakeven_stop": ("BE", "low", "shield"),
    "trail_stop": ("TRAIL", "low", "arrow_up"),
    "stopped_out": ("STOP", "high", "stop_sign"),
    "force_close": ("EOD Force Close", "high", "bell"),
    "trade_failed": ("Trade FAILED", "urgent", "warning"),
    "trade_timeout": ("Trade TIMEOUT", "urgent", "warning"),
    "state_corrupt": ("State file corrupt", "urgent", "warning"),
}


def notify(title: str, message: str, priority: str = "default", tags: str = "") -> None:
    topic = os.getenv("NTFY_TOPIC", "").strip()
    if not topic:
        return
    try:
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": tags},
            timeout=5,
        )
    except Exception as e:
        try:
            from datetime import datetime
            from pathlib import Path as _P
            d = _P(__file__).resolve().parent.parent / "logs"
            d.mkdir(parents=True, exist_ok=True)
            with (d / "notify_errors.log").open("a", encoding="utf-8") as f:
                f.write(f"{datetime.now().isoformat()} {title!r}: {type(e).__name__}: {e}\n")
        except Exception:
            pass


def notify_event(event: str, fields: dict) -> None:
    if event not in ALERT_EVENTS:
        return
    title, prio, tags = ALERT_EVENTS[event]
    sym = fields.get("symbol", "")
    body = ", ".join(f"{k}={v}" for k, v in fields.items() if k not in {"ctx", "stderr"})
    notify(f"{title} {sym}".strip(), body or event, prio, tags)
