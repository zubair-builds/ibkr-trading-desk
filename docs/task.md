# IBKR Bot Task List (PM Framework)

## 🔴 ACTIVE EPIC: Full Automation & Analytics
**Goal:** Allow the bot to execute trades without human intervention and monitor its own performance.


- [ ] **Task 3.1E: Targeted AI Analysis (Ticker Selection)**
  - **DoD**: Add an input field/dropdown to the Manual Test UI so the user can force the AI to analyze a *specific* ticker (e.g., TSLA) rather than picking one from the entire portfolio context.
- [ ] **Task 3.2: Notifications & Alerting (Discord/Slack)**
  - **DoD**: A webhook is triggered on every execution, pinging a chat app with the fill price and P&L.
- [ ] **Task 3.3: Advanced Portfolio Analytics**
  - **DoD**: The dashboard contains a visual pie chart for asset allocation and a time-series graph for P&L tracking.

we should add a section to the dashboard to display the AI's thinking process and the reasoning behind its decisions.

## 🔍 OPEN ISSUES & REFINEMENTS FOR AUTOTRADE

### 🔴 Autopilot (AI Trading)
- [x] **Fix Guard Rail Logic**: Currently blocked BUY orders with "Invalid Price Data". Need to ensure we don't block valid trades due to temporary quote unavailability or data format nuances.
- [x] **Add Stop Loss / Take Profit**: The "Buy" order should be accompanied by a contingent Stop Loss and Take Profit order to manage risk, as discussed.
- [ ] **Context Builder Coverage**: Ensure `build_market_context` fetches data for all positions and watchlist symbols, not just those with open orders or in the watchlist.

### 🟡 Market Maker (MM Strategy)
- [ ] **Add Stop/Loss & T/P to MM Orders**: MM orders should also have intelligent Stop Loss and Take Profit orders attached to them.
- [ ] **Improve Quote Reliability**: MM strategy relies heavily on "Last Price". Should we use "Ask Price" for BUYs and "Bid Price" for SELLs to be safer/faster? Or a combination?
- [ ] **Handle Grid Adjustment**: Allow manual adjustment of the "Grid Size" (spread width) via the UI.

### 🔵 General System
- [ ] **IB Gateway / IBC Status**: The UI does not yet show the connection status (e.g., Connected to IB Gateway, Watchdog Active). This is critical for trust.
- [ ] **Execution Log Fidelity**: The "Recent Executions" table should show the status (Filled, Cancelled) and ideally the time (HH:MM:SS).

---

## ⏸️ UPCOMING EPIC 4: Production Safety & Risk Management 
**Goal:** Ensure the bot has necessary guardrails, monitoring, and security before ever trading with real money or running entirely unsupervised.

- `[/]` **Task 4.0: Local IBC Setup & Testing (No Docker)**
  - **DoD**: Install IB Controller (IBC) natively on macOS to automate IB Gateway login locally and test the "headless" flow before later containerizing it.
  - **Status**: Pending user to install offline IB Gateway 10.19 to ~/Applications/IBJts. (xterm issue in start_gateway.sh is fixed).
- `[ ]` **Task 4.1: The "Kill Switch" & Risk Limits**
  - **DoD**: A global UI button that instantly disconnects the broker and cancels pending orders. Hardcoded limits (Max daily loss, Max orders/min) exist in the backend.
- [ ] **Task 4.2: System Logging UI**
  - **DoD**: A dedicated dashboard tab streams backend Python logs and AI thinking/raw prompts in real-time.
- [ ] **Task 4.4: System Health & Connection Indicator**
  - **DoD**: A persistent global UI element (e.g., in the navbar) that displays the live connection status (Connected/Disconnected) of the Python backend to the IB Gateway, including error states, so the user has immediate feedback when running headlessly.

---

## ⏸️ UPCOMING EPIC 5: UI & Aesthetics
**Goal:** Polish the frontend to have a more professional and customized appearance.