import logging

from typing import Optional
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import os
import secrets
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from bot.ib_service import HistoricalDataError, IBService, PacingLimitError
from bot.ai_context import build_market_context
from bot.ai_agent import analyze_market, TradeSignal
from bot.autotrade import autotrade_manager

from fastapi import APIRouter
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

security = HTTPBasic()

def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = secrets.compare_digest(credentials.username, os.getenv("DASHBOARD_USER", "admin"))
    correct_password = secrets.compare_digest(credentials.password, os.getenv("DASHBOARD_PASS", "admin"))
    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=401,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

app = FastAPI(title="IBKR Bot API")
protected_router = APIRouter(dependencies=[Depends(verify_credentials)])


class OrderRequest(BaseModel):
    symbol: str
    action: str
    quantity: float
    order_type: str = "MKT"
    lmt_price: Optional[float] = None
    outside_rth: bool = False

class OrderModifyRequest(BaseModel):
    quantity: float
    lmt_price: Optional[float] = None


# Enable CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # For development, allow all. Restrict in prod.
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_ib_service(request: Request) -> IBService:
    """
    FastAPI dependency to retrieve the single IBService instance
    attached to the application state in main.py.
    """
    ib_service = getattr(request.app.state, "ib_service", None)
    if ib_service is None:
        raise HTTPException(status_code=500, detail="IB service not initialized")
    return ib_service


@app.get("/health")
async def health_check(ib_service: IBService = Depends(get_ib_service)):
    return ib_service.health()


@app.post("/connect")
async def connect_ib(ib_service: IBService = Depends(get_ib_service)):
    # Preserve legacy semantics as closely as possible.
    health = ib_service.health()
    if health.get("status") == "connected" and health.get("operational"):
        return {"status": "already_connected", "message": "Bot is already connected"}

    ib_service.request_reconnect()
    return {"status": "success", "message": "Reconnection requested"}


@app.get("/account")
async def get_account_summary(ib_service: IBService = Depends(get_ib_service)):
    try:
        summary = ib_service.get_account_summary()
        return summary
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/orders")
async def get_orders(ib_service: IBService = Depends(get_ib_service)):
    return ib_service.get_orders()


@app.get("/executions")
async def get_executions(ib_service: IBService = Depends(get_ib_service)):
    return ib_service.get_executions()


@app.post("/order")
async def place_order(order_req: OrderRequest, ib_service: IBService = Depends(get_ib_service)):
    try:
        trade = ib_service.place_order(
            symbol=order_req.symbol,
            action=order_req.action,
            quantity=order_req.quantity,
            order_type=order_req.order_type,
            lmt_price=order_req.lmt_price,
            outside_rth=order_req.outside_rth,
        )
        return {"status": "success", "trade": trade}
    except ValueError as e:
        logger.warning("POST /order validation error: %s", e)
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        logger.warning("POST /order runtime error: %s", e)
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception("POST /order internal error: %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/order/{order_id}/cancel")
async def cancel_order(order_id: int, ib_service: IBService = Depends(get_ib_service)):
    try:
        return ib_service.cancel_order(order_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/order/{order_id}/modify")
async def modify_order(order_id: int, req: OrderModifyRequest, ib_service: IBService = Depends(get_ib_service)):
    try:
        return ib_service.modify_order(order_id, req.quantity, req.lmt_price)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/positions")
async def get_positions(ib_service: IBService = Depends(get_ib_service)):
    return ib_service.get_positions()


@app.get("/quote")
def get_quote(symbol: str, ib_service: IBService = Depends(get_ib_service)):
    """
    Return a simple quote snapshot for the requested symbol.
    """
    if not symbol:
        logger.warning("GET /quote: missing symbol parameter")
        raise HTTPException(status_code=400, detail="Query parameter 'symbol' is required")

    try:
        quote = ib_service.get_quote(symbol)
        if isinstance(quote, dict) and quote.get("error"):
            logger.warning("GET /quote symbol=%s: %s", symbol, quote["error"])
            raise HTTPException(status_code=500, detail=quote["error"])
        logger.info("GET /quote symbol=%s: 200 OK", symbol)
        return quote
    except HTTPException:
        raise
    except RuntimeError as e:
        logger.warning("GET /quote symbol=%s: 503 %s", symbol, e)
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        logger.warning("GET /quote symbol=%s: 400 %s", symbol, e)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("GET /quote symbol=%s: 500 %s", symbol, e)
        raise HTTPException(status_code=500, detail=str(e))


@protected_router.get("/historical")
def get_historical(
    symbol: str,
    duration: str = "1 M",
    bar_size: str = "1 day",
    what_to_show: str = "TRADES",
    use_rth: bool = True,
    end_datetime: str = "",
    primary_exchange: str = "",
    ib_service: IBService = Depends(get_ib_service),
):
    """
    Get historical market data for a symbol.

    Args:
        symbol: Stock symbol (e.g., AAPL)
        duration: How far back to fetch (e.g., "1 M", "1 Y", "5 D")
        bar_size: Bar size (e.g., "1 day", "1 hour", "5 mins")
        what_to_show: TRADES (default, no dividends), ADJUSTED_LAST
            (split+dividend adjusted -- requires end_datetime blank),
            MIDPOINT, BID, ASK, BID_ASK, etc.
        use_rth: Regular trading hours only (default True).
        end_datetime: IB datetime string for the end of the window, or
            blank (default) for "now". Required blank for ADJUSTED_LAST.
        primary_exchange: Disambiguates SMART-routed symbols listed on
            multiple exchanges.

    Returns {"bars": [...], "meta": {...}} -- never a bare 200 with an
    empty list; IB reporting zero bars is surfaced as a 502 instead.
    """
    if not symbol:
        logger.warning("GET /historical: missing symbol parameter")
        raise HTTPException(status_code=400, detail="Query parameter 'symbol' is required")

    try:
        data = ib_service.get_historical_data(
            symbol=symbol,
            duration=duration,
            bar_size=bar_size,
            what_to_show=what_to_show,
            use_rth=use_rth
        )
        logger.info(
            "GET /historical symbol=%s duration=%s bar_size=%s what_to_show=%s: 200 OK (%d bars)",
            symbol, duration, bar_size, what_to_show, data["meta"]["bar_count"],
        )
        return data
    except Exception as e:
        logger.exception("GET /historical symbol=%s: 500 %s", symbol, e)
        raise HTTPException(status_code=500, detail=str(e))


@protected_router.get("/watchlist")
def get_watchlist(ib_service: IBService = Depends(get_ib_service)):
    return ib_service.get_watchlist()


@protected_router.post("/watchlist")
def add_to_watchlist(symbol: str, ib_service: IBService = Depends(get_ib_service)):
    return ib_service.add_to_watchlist(symbol)


@protected_router.delete("/watchlist/{symbol}")
def remove_from_watchlist(symbol: str, ib_service: IBService = Depends(get_ib_service)):
    return ib_service.remove_from_watchlist(symbol)


@protected_router.get("/ai/context")
def get_ai_context(ib_service: IBService = Depends(get_ib_service)):
    """
    Returns the LLM-friendly minified JSON context.
    """
    try:
        context_str = build_market_context(ib_service)
        # Parse it back to a dict so FastAPI serves it correctly as JSON, 
        # but returning it as raw response also works.
        import json
        return json.loads(context_str)
    except Exception as e:
        logger.exception("GET /ai/context error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@protected_router.post("/ai/analyze", response_model=TradeSignal)
def run_ai_analysis(ib_service: IBService = Depends(get_ib_service)):
    """
    Triggers the AI to analyze the current market context and return a structured trade signal.
    """
    try:
        signal = analyze_market(ib_service)
        
        if signal.action in ["BUY", "SELL"] and signal.quantity > 0:
            tp_pct = 0.10 if signal.action == "BUY" else None
            sl_pct = 0.05 if signal.action == "BUY" else None
            try:
                res = ib_service.place_order(
                    symbol=signal.symbol,
                    action=signal.action,
                    quantity=signal.quantity,
                    order_type="MKT",
                    take_profit_pct=tp_pct,
                    stop_loss_pct=sl_pct,
                    transmit=False
                )
                signal.draft_order_status = f"Draft Order Submitted ({res.get('status', 'Unknown')})"
            except Exception as e:
                logger.warning(f"Draft order failed: {e}")
                signal.draft_order_status = f"Draft Order Failed: {e}"

        return signal
    except Exception as e:
        logger.exception("POST /ai/analyze error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


class AutotradeConfigRequest(BaseModel):
    enabled: bool
    interval_minutes: int

@protected_router.get("/autotrade/config")
def get_autotrade_config():
    return autotrade_manager.get_config()

@protected_router.post("/autotrade/config")
def update_autotrade_config(req: AutotradeConfigRequest):
    autotrade_manager.update_config(req.enabled, req.interval_minutes)
    return autotrade_manager.get_config()

app.include_router(protected_router)


@app.websocket("/ws/marketdata")
async def websocket_marketdata(websocket: WebSocket):
    ib_service = websocket.app.state.ib_service
    await websocket.accept()
    subscriptions = set()
    import asyncio
    loop = asyncio.get_running_loop()
    
    def send_quote(quote_dict):
        asyncio.run_coroutine_threadsafe(websocket.send_json(quote_dict), loop)
        
    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")
            symbol = data.get("symbol")
            if action == "subscribe" and symbol:
                subscriptions.add(symbol)
                ib_service.subscribe_ticker_updates(symbol, send_quote)
            elif action == "unsubscribe" and symbol:
                if symbol in subscriptions:
                    subscriptions.remove(symbol)
                    ib_service.unsubscribe_ticker_updates(symbol, send_quote)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        for symbol in subscriptions:
            ib_service.unsubscribe_ticker_updates(symbol, send_quote)


# Mount static assets directory
app.mount("/assets", StaticFiles(directory="dashboard/dist/assets"), name="assets")

# Catch-all route to serve the React SPA
@app.get("/{catchall:path}")
def serve_spa(catchall: str):
    file_path = os.path.join("dashboard/dist", catchall)
    if os.path.isfile(file_path):
        return FileResponse(file_path)
    return FileResponse("dashboard/dist/index.html")
