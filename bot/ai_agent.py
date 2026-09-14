import os
import json
import logging
from typing import Optional
from pydantic import BaseModel
from google import genai
from google.genai import types

from bot.ib_service import IBService
from bot.ai_context import build_market_context

logger = logging.getLogger(__name__)

class TradeSignal(BaseModel):
    symbol: str
    action: str  # e.g., BUY, SELL, HOLD
    quantity: float
    reasoning: str
    draft_order_status: Optional[str] = None

def analyze_market(ib_service: IBService) -> TradeSignal:
    """
    Grabs the latest market context and sends it to Gemini for a structured trade signal.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY environment variable is not set")

    model_id = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    client = genai.Client(api_key=api_key)
    
    # 1. Grab the context string
    context_str = build_market_context(ib_service)
    
    # 2. Build the prompt
    prompt = f"""
    You are a highly sophisticated autonomous AI trading agent.
    Your goal is to maximize portfolio return while managing risk. 
    Analyze the following live market context which includes:
    - Account summary (Total Cash, Buying Power, Net Liquidation)
    - Current open positions (with unrealized PnL and average cost)
    - Open pending orders
    - Recent daily OHLCV (Open, High, Low, Close, Volume) data for watchlist and portfolio symbols.
    
    Market Context:
    {context_str}
    
    Instructions:
    1. Evaluate the market data and your current portfolio exposure.
    2. Decide on ONE single trade action to take right now for ONE symbol. 
    3. If no trade is appealing, output action "HOLD" with 0 quantity.
    4. Provide a short, logical reasoning for your decision.
    """

    # 3. Call Gemini with Structured Outputs
    logger.info(f"Calling Gemini ({model_id}) for market analysis...")
    response = client.models.generate_content(
        model=model_id,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=TradeSignal,
            temperature=0.2, # Low temperature for more deterministic trading decisions
        ),
    )
    
    if not response.text:
        raise RuntimeError("Received empty response from Gemini API")
        
    # 4. Parse response back into Pydantic model
    signal_dict = json.loads(response.text)
    return TradeSignal(**signal_dict)
