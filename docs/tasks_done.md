# Completed Tasks

- [x] **Task 3.4: Fix Memory Leak & OOM Issue on Render**: Patched `ib_service.py` to clear `ib-insync`'s ticker cache when fetching quotes to prevent unbounded memory growth. Added `TWS_MAX_HEAP` constraint to `render.yaml` and `docker-compose.yml` to restrict IB Gateway Java memory allocation.
- [x] **Fix Market Data Subscription Errors**: Resolved data subscription errors by falling back to delayed data where necessary.
- [x] **Order Placement UI**: Built the Trade Center to allow submitting Limit/Market buy/sell orders directly from the React dashboard.
- [x] **Docker Deployment Setup**: (Part of Task 4.3) Created the "All-in-One" single-container Docker setup and `start_all.sh` orchestrator for cloud deployment.
- [x] **Task 4.3: Security & Deployment**: Deployed the "All-in-One" Docker container to Render and verified end-to-end functionality. Dashboard is protected by basic authentication.
- [x] **Task 1.3: Trade Execution History Database**: The backend saves all fills to a local SQLite `trades.db`. The dashboard's History tab fetches from this DB to show lifetime trades, surviving restarts.
- [x] **Task 5.1: Update Favicon**: The default Vite favicon was replaced with a custom-generated futuristic trading bot icon in `dashboard/public/favicon.png` and updated in `index.html`.
- [x] **Task 1.3: Extended Hours Trading Support**: The API and dashboard UI support a toggle for `outsideRth` to allow limit orders to execute during pre-market and post-market hours.
- [x] **Task 1.2: Real-time Price Streaming (WebSockets)**: Added WebSocket endpoints and a Live Quote component to stream live prices to the Trade Center instantly.
- [x] **Task 1.1: Order Modification & Cancellation UI**: UI buttons exist to cancel pending orders or modify their limit prices. A canceled order immediately reflects in the History tab without page refresh.
- [x] **Task 2.1: Data Formatting Service**: Backend script that securely grabs portfolio state + recent market candles and formats it as an LLM-friendly JSON string.
- [x] **Task 2.2: Gemini API Integration**: Backend endpoint that sends the context to Gemini and successfully parses its text response into a structured Buy/Sell/Hold signal.
- [x] **Task 3.1: Automated Trading Loop**: A background task that runs every X minutes, evaluates the AI's signal, and executes the trade automatically.
- [x] **Task 3.1A: Fix AI Context Data Mapping**: Fixed `ai_context.py` to ensure `AvailableFunds` and `BuyingPower` are correctly parsed and explicitly understood by Gemini.
- [x] **Task 3.1B: Manual AI Test Button**: Added a "Test AI Analysis" button to the Autopilot panel.
- [x] **Task 3.1C: AI Harness & Pre-Trade Guard Rails**: Implemented strict guard rails.
- [x] **Task 3.1D: Draft Orders for Manual Testing**: Modified the Manual AI Test to submit the AI's signal to IBKR as a Draft order (`transmit=False`) with its bracket orders.
