# IBKR Trading Desk

Personal trading stack for Interactive Brokers: headless IB Gateway in Docker, a FastAPI + `ib-insync` backend, and a React dashboard served from the same origin.

Paper mode is the default. This is not a hosted product and not financial advice.

## What it does

- Runs IB Gateway headless (Xvfb + IBC) in one container
- REST API for account, positions, history, and orders (`bot/`)
- Optional autotrade + Gemini helper (`bot/autotrade.py`, `bot/ai_agent.py`)
- Vite + React dashboard (positions, watchlist, charts) mounted at `:8000`
- Offline helpers: `ingest.py`, `backtest.py`
- Render blueprint (`render.yaml`) for a single web service

## Stack

| Layer | Choice |
| --- | --- |
| Broker | Interactive Brokers Gateway (paper or live) |
| Broker client | Python, `ib-insync` |
| API | FastAPI + Uvicorn, HTTP Basic Auth |
| UI | React 19, Vite, TypeScript, lightweight-charts |
| Config | `config/settings.yaml`, `.env` |
| Run | Docker Compose, multi-stage Dockerfile |
| Deploy | Render (`render.yaml`) |

```text
browser  --Basic Auth-->  FastAPI :8000  -->  static React build
                                 |
                                 +--> ib-insync TCP --> IB Gateway --> IBKR
```

More detail: [docs/architecture.md](docs/architecture.md).

## Setup

Needs Docker and an IBKR paper (or live) account.

```bash
git clone https://github.com/zubair-builds/ibkr.git
cd ibkr
cp .env.example .env
# set TWS_USERID, TWS_PASSWORD, DASHBOARD_USER, DASHBOARD_PASS
docker compose up --build -d
```

- Dashboard: [http://localhost:8000](http://localhost:8000)
- API docs: [http://localhost:8000/docs](http://localhost:8000/docs)
- Gateway VNC (debug): `:5900` if `VNC_PASSWORD` is set

`TRADING_MODE=paper` uses gateway port 4004; `live` uses 4003. Keep live credentials out of git. Do not commit `.env`.

Local UI-only work (API already running): `cd dashboard && npm install && npm run dev`.

## Layout

```text
bot/           FastAPI app, IB service, autotrade, AI helper
dashboard/     Vite + React UI
config/        settings.yaml
scripts/       container entry helpers
ingest.py      data ingest
backtest.py    simple backtest
Dockerfile     gateway + API + built UI
docker-compose.yml
render.yaml
```

## What I would do next

- Tests around order placement and reconnect
- Cancel / modify orders in the UI
- Structured logs instead of container stdout only
- Rename this repo to `ibkr-trading-desk` (GitHub Settings; not done from CI)

## Author

[Syed Zubair Haider](https://github.com/zubair-builds) · [LinkedIn](https://www.linkedin.com/in/syed-zubair-haider/)
