from ib_insync import IB, Stock, MarketOrder, Trade

SETTLE_TIMEOUT_S = 10
PENDING = {"PendingSubmit", "ApiPending", "PreSubmitted", ""}


class IBKRClient:
    def __init__(self, host: str, port: int, client_id: int):
        self.ib = IB()
        self.ib.connect(host, port, clientId=client_id)

    def place_order(self, symbol: str, side: str, quantity: int) -> Trade:
        contract = Stock(symbol, "SMART", "USD")
        qualified = self.ib.qualifyContracts(contract)
        if not qualified:
            raise ValueError(f"could not qualify contract for {symbol}")
        order = MarketOrder(side.upper(), quantity)
        order.outsideRth = True
        trade = self.ib.placeOrder(qualified[0], order)
        waited = 0.0
        while waited < SETTLE_TIMEOUT_S:
            if trade.orderStatus.status not in PENDING or trade.isDone():
                break
            self.ib.sleep(0.5)
            waited += 0.5
        return trade

    def disconnect(self):
        self.ib.disconnect()
