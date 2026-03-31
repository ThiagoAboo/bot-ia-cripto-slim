from __future__ import annotations

import queue
import threading
from typing import Any

import ccxt

from .utils import clamp, iso_now, safe_float


class ExecutorService:
    def __init__(self, db, config_manager, runtime_state, tracer):
        self.db = db
        self.config_manager = config_manager
        self.runtime_state = runtime_state
        self.tracer = tracer
        self.orders: queue.Queue = queue.Queue(maxsize=2000)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name="executor", daemon=True)
        self.exchange = None

    def start(self) -> None:
        self.ensure_wallet()
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=5)

    def enqueue(self, order: dict[str, Any]) -> None:
        try:
            self.orders.put_nowait(order)
        except queue.Full:
            self.tracer.trace("executor", "error", level="ERROR", message="Fila de ordens cheia")

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                order = self.orders.get(timeout=1)
            except queue.Empty:
                order = None
            if not order:
                continue
            try:
                self.process_order(order)
            except Exception as exc:
                self.tracer.trace("executor", "error", symbol=order.get("symbol"), level="ERROR", message="Erro ao executar ordem", data={"error": str(exc), "order": order})

    def ensure_wallet(self) -> None:
        config = self.config_manager.get()
        if not self.db.get_balance("USDT"):
            self.db.reset_simulated_wallet(config["risk"]["simulated_initial_balance"])

    def process_order(self, signal: dict[str, Any]) -> None:
        config = self.config_manager.get()
        mode = config["general"]["trade_mode"]
        symbol = signal["symbol"]
        price = self.runtime_state.latest_prices.get(symbol)
        if not price:
            self.tracer.trace("executor", "rule_filter", symbol=symbol, message="Sem preço atual para ordem")
            return

        side = signal["side"].lower()
        if mode == "simulated":
            order = self.execute_simulated(symbol=symbol, side=side, price=price, confidence=signal["confidence"], reason=signal["reason"])
        else:
            order = self.execute_real(symbol=symbol, side=side, price=price, confidence=signal["confidence"], reason=signal["reason"])

        self.runtime_state.last_order_update = iso_now()
        self.tracer.trace("executor", "order", symbol=symbol, message="Ordem processada", data=order)

    def execute_simulated(self, symbol: str, side: str, price: float, confidence: float, reason: str) -> dict[str, Any]:
        config = self.config_manager.get()
        base_asset = symbol.replace("USDT", "")
        wallet = {row["asset"]: row for row in self.db.list_balances()}
        usdt = wallet.get("USDT", {"free": 0.0})
        base = wallet.get(base_asset, {"free": 0.0, "avg_price": 0.0})
        bnb = wallet.get("BNB", {"free": 0.0})

        order_percent = float(config["risk"]["default_order_percent"])
        min_order_usdt = float(config["risk"]["min_order_usdt"])
        max_position_per_symbol = float(config["risk"]["max_position_per_symbol"])
        total_equity = self.portfolio_value()
        symbol_position_value = safe_float(base.get("free")) * price
        max_symbol_value = total_equity * max_position_per_symbol

        fee_rate = float(config["binance"]["taker_fee_percent"]) / 100.0
        fee_asset = "BNB" if config["binance"]["use_bnb_for_fees"] else "USDT"
        fee_amount = 0.0
        status = "simulated"
        pnl = 0.0
        quantity = 0.0

        if side == "buy":
            available_to_spend = min(safe_float(usdt["free"]) * order_percent, max(0.0, max_symbol_value - symbol_position_value))
            if available_to_spend < min_order_usdt:
                status = "blocked"
            else:
                quantity = available_to_spend / price
                fee_amount = available_to_spend * fee_rate / max(self.runtime_state.latest_prices.get("BNBUSDT", 600.0), 1) if fee_asset == "BNB" else available_to_spend * fee_rate
                new_usdt = safe_float(usdt["free"]) - available_to_spend
                new_qty = safe_float(base["free"]) + quantity
                avg_price = ((safe_float(base["free"]) * safe_float(base.get("avg_price", 0))) + available_to_spend) / max(new_qty, 1e-9)
                self.db.upsert_balance("USDT", new_usdt, avg_price=1)
                self.db.upsert_balance(base_asset, new_qty, avg_price=avg_price)
                self._apply_fee(fee_asset, fee_amount)
        else:
            reserve = float(config["binance"]["bnb_reserve"]) if base_asset == "BNB" else 0.0
            sellable = max(0.0, safe_float(base["free"]) - reserve)
            quantity = sellable * order_percent
            if quantity * price < min_order_usdt or quantity <= 0:
                status = "blocked"
            else:
                proceeds = quantity * price
                fee_amount = proceeds * fee_rate / max(self.runtime_state.latest_prices.get("BNBUSDT", 600.0), 1) if fee_asset == "BNB" else proceeds * fee_rate
                avg_price = safe_float(base.get("avg_price", 0))
                pnl = (price - avg_price) * quantity
                self.db.upsert_balance(base_asset, safe_float(base["free"]) - quantity, avg_price=avg_price)
                self.db.upsert_balance("USDT", safe_float(usdt["free"]) + proceeds, avg_price=1)
                self._apply_fee(fee_asset, fee_amount)

        order = {
            "created_at": iso_now(),
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": price,
            "fee_asset": fee_asset,
            "fee_amount": fee_amount,
            "mode": "simulated",
            "status": status,
            "confidence": confidence,
            "reason": reason,
            "pnl_usdt": pnl,
            "metadata": {"wallet_value": self.portfolio_value()},
        }
        self.db.insert_order(order)
        return order

    def _apply_fee(self, fee_asset: str, fee_amount: float) -> None:
        if fee_amount <= 0:
            return
        balance = self.db.get_balance(fee_asset) or {"free": 0.0, "avg_price": 0.0}
        self.db.upsert_balance(fee_asset, max(0.0, safe_float(balance["free"]) - fee_amount), avg_price=safe_float(balance.get("avg_price", 0.0)))

    def execute_real(self, symbol: str, side: str, price: float, confidence: float, reason: str) -> dict[str, Any]:
        config = self.config_manager.get()
        exchange = self._exchange()
        balance = exchange.fetch_balance()
        usdt = safe_float(balance["free"].get("USDT"))
        order_percent = float(config["risk"]["default_order_percent"])
        qty = 0.0

        if side == "buy":
            qty = (usdt * order_percent) / price
            order_resp = exchange.create_market_buy_order(symbol.replace("USDT", "/USDT"), qty)
        else:
            base_asset = symbol.replace("USDT", "")
            free_qty = safe_float(balance["free"].get(base_asset))
            if base_asset == "BNB":
                reserve = float(config["binance"]["bnb_reserve"])
                free_qty = max(0.0, free_qty - reserve)
                self.tracer.trace("executor", "bnb_management", symbol=symbol, message="Aplicada reserva de BNB", data={"reserve": reserve})
            qty = free_qty * order_percent
            order_resp = exchange.create_market_sell_order(symbol.replace("USDT", "/USDT"), qty)

        order = {
            "created_at": iso_now(),
            "symbol": symbol,
            "side": side,
            "quantity": qty,
            "price": price,
            "fee_asset": "exchange",
            "fee_amount": 0.0,
            "mode": "real",
            "status": "executed",
            "confidence": confidence,
            "reason": reason,
            "metadata": {"exchange_response": order_resp},
        }
        self.db.insert_order(order)
        return order

    def _exchange(self):
        if self.exchange:
            return self.exchange
        config = self.config_manager.get()["binance"]
        self.exchange = ccxt.binance(
            {
                "apiKey": config["api_key"],
                "secret": config["api_secret"],
                "enableRateLimit": True,
                "options": {"defaultType": "spot"},
            }
        )
        return self.exchange

    def portfolio_value(self) -> float:
        total = 0.0
        for row in self.db.list_balances():
            asset = row["asset"]
            free = safe_float(row["free"])
            if asset == "USDT":
                total += free
            else:
                symbol = f"{asset}USDT"
                total += free * safe_float(self.runtime_state.latest_prices.get(symbol), 0.0)
        return total

    def open_positions(self) -> list[dict[str, Any]]:
        positions = []
        for row in self.db.list_balances():
            asset = row["asset"]
            if asset == "USDT" or safe_float(row["free"]) <= 0:
                continue
            symbol = f"{asset}USDT"
            current_price = safe_float(self.runtime_state.latest_prices.get(symbol), 0.0)
            avg_price = safe_float(row.get("avg_price", 0.0))
            pnl = (current_price - avg_price) * safe_float(row["free"])
            variation_pct = ((current_price - avg_price) / avg_price * 100.0) if avg_price else 0.0
            positions.append(
                {
                    "symbol": symbol,
                    "quantity": free if (free := safe_float(row["free"])) else 0.0,
                    "avg_price": avg_price,
                    "current_price": current_price,
                    "variation_pct": variation_pct,
                    "pnl_usdt": pnl,
                }
            )
        positions.sort(key=lambda item: item["pnl_usdt"], reverse=True)
        return positions
