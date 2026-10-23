"""Weekday scheduler: prefilter at 09:45 ET, cycle.py every 5 min 10:00-15:55 ET, perf+rotate 16:05 ET."""
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent
PY = sys.executable


def say(m):
    print(f"[{datetime.now(ET):%Y-%m-%d %H:%M:%S} ET] scheduler: {m}", flush=True)


def run(script, timeout):
    say(f"running {script}")
    try:
        p = subprocess.run([PY, str(ROOT / script)], cwd=ROOT, timeout=timeout, capture_output=True, text=True)
        out = (p.stdout or "") + (p.stderr or "")
        for line in out.splitlines()[-40:]:
            print("   " + line, flush=True)
        say(f"{script} exit={p.returncode}")
    except subprocess.TimeoutExpired:
        say(f"{script} TIMEOUT after {timeout}s")


def main():
    last_prefilter_date = None
    last_cycle_slot = None
    last_eod_date = None
    say("started")
    while True:
        now = datetime.now(ET)
        if now.weekday() < 5:
            hm = now.hour * 60 + now.minute
            if 9 * 60 + 45 <= hm < 10 * 60 and last_prefilter_date != now.date():
                run("morning_prefilter.py", 180)
                last_prefilter_date = now.date()
            if 10 * 60 <= hm < 16 * 60:
                slot = (now.date(), hm // 5)
                if slot != last_cycle_slot:
                    last_cycle_slot = slot
                    run("cycle.py", 240)
            if 16 * 60 + 5 <= hm < 16 * 60 + 30 and last_eod_date != now.date():
                last_eod_date = now.date()
                run("compute_perf.py", 120)
                run("rotate_logs.py", 120)
        time.sleep(15)


if __name__ == "__main__":
    main()
