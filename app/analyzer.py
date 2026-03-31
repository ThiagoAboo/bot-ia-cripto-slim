from __future__ import annotations

import threading
import time
from collections import deque
from datetime import UTC, datetime
from typing import Any

from .collector import TIMEFRAME_TO_INTERVAL
from .models import FEATURE_ORDER
from .utils import clamp, iso_now, mean, pct_change, stddev, z_score


class AnalyzerService:
    def __init__(self, db, config_manager, runtime_state, tracer, collector):
        self.db = db
        self.config_manager = config_manager
        self.runtime_state = runtime_state
        self.tracer = tracer
        self.collector = collector
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name="analyzer", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=5)

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.calculate_all()
                self.runtime_state.last_feature_update = iso_now()
            except Exception as exc:
                self.tracer.trace("analyzer", "error", level="ERROR", message="Erro na análise", data={"error": str(exc)})
            self.stop_event.wait(5)

    def calculate_all(self) -> None:
        config = self.config_manager.get()

        # remove strong symbols expirados
        now = datetime.now(UTC)
        expired = [symbol for symbol, expiry in self.runtime_state.strong_symbols.items() if datetime.fromisoformat(expiry) < now]
        for symbol in expired:
            self.runtime_state.strong_symbols.pop(symbol, None)

        if config["general"]["dynamic_symbols_enabled"]:
            active = set(self.config_manager.get_symbols().get("base_symbols", []))
            if config["social"]["auto_add_strong_symbols"] and config["general"]["auto_trade_social"]:
                active.update(self.runtime_state.strong_symbols.keys())
            self.runtime_state.active_symbols = active

        for symbol in sorted(self.runtime_state.active_symbols):
            feature = self.calculate_features_for_symbol(symbol)
            if feature:
                self.runtime_state.latest_features[symbol] = feature
                self.db.insert_feature(timestamp=feature["timestamp"], symbol=symbol, data=feature)
                self.tracer.trace("analyzer", "feature", symbol=symbol, message="Features atualizadas", data=feature)

    def calculate_features_for_symbol(self, symbol: str) -> dict[str, Any] | None:
        config = self.config_manager.get()
        current_price = self.runtime_state.latest_prices.get(symbol)
        if not current_price:
            candles = self.db.get_recent_candles(symbol, "1m", 2)
            if candles:
                current_price = candles[-1]["close"]
        if not current_price:
            return None

        zscore_period = int(config["analysis"]["value_zscore_periods"])
        recent_1m = self.db.get_recent_candles(symbol, "1m", max(60, zscore_period + 5))
        close_prices = [row["close"] for row in recent_1m if row.get("close") is not None]
        value_z = z_score(current_price, close_prices[-zscore_period:]) if close_prices else 0.0

        micro_scores = []
        for item in config["analysis"]["micro_trade_windows"]:
            window = int(item["window_seconds"])
            weight = float(item["weight"])
            previous = self._price_seconds_ago(symbol, window)
            micro_scores.append((pct_change(current_price, previous), weight))
        micro_weight_total = sum(weight for _, weight in micro_scores) or 1.0
        micro_score = sum(score * weight for score, weight in micro_scores) / micro_weight_total

        momentum_values = []
        for timeframe in config["analysis"]["momentum_resolutions"]:
            candles = self.db.get_recent_candles(symbol, timeframe, 30)
            if len(candles) >= 2:
                momentum_values.append(pct_change(candles[-1]["close"], candles[0]["close"]))
        standard_score = mean(momentum_values)

        momentum_weights = config["analysis"]["momentum_weights"]
        momentum_score = (micro_score * float(momentum_weights["micro"])) + (standard_score * float(momentum_weights["standard"]))

        book = self.runtime_state.latest_book.get(symbol, {})
        volume_24h = max(book.get("volume_24h") or 1.0, 1.0)
        liquidity_score = clamp(((book.get("bid_liquidity", 0.0) + book.get("ask_liquidity", 0.0)) / volume_24h) * 10, 0.0, 1.0)

        bid_liq = book.get("bid_liquidity", 0.0)
        ask_liq = book.get("ask_liquidity", 0.0)
        total_liq = max(bid_liq + ask_liq, 1e-9)
        imbalance = clamp((bid_liq - ask_liq) / total_liq, -1.0, 1.0)

        recent_volumes = [row["volume"] for row in recent_1m[-24:] if row.get("volume") is not None]
        current_volume = recent_1m[-1]["volume"] if recent_1m else 0.0
        avg_volume = mean(recent_volumes)
        volume_std = stddev(recent_volumes)
        threshold = float(config["analysis"]["volume_anomaly_threshold"])
        volume_anomaly = 0.0
        if volume_std > 0:
            volume_anomaly = clamp(((current_volume - avg_volume) / volume_std) / max(threshold, 0.01), -3.0, 3.0) / 3.0

        social_score = self.runtime_state.latest_social_scores.get(symbol, 0.0)
        news_sentiment_score = self._news_score(symbol)
        spread_score = clamp(1 - min(book.get("spread", 0.0), 0.02) / 0.02, 0.0, 1.0)
        volatility_score = self._volatility_score(close_prices[-20:])

        feature = {
            "timestamp": iso_now(),
            "symbol": symbol,
            "price": current_price,
            "value_zscore": value_z,
            "micro_momentum_score": micro_score,
            "standard_momentum_score": standard_score,
            "momentum_score": momentum_score,
            "social_score": social_score,
            "liquidity_score": liquidity_score,
            "order_imbalance_score": imbalance,
            "volume_anomaly_score": volume_anomaly,
            "news_sentiment_score": news_sentiment_score,
            "spread_score": spread_score,
            "volatility_score": volatility_score,
        }
        feature["label"] = self._derive_label(symbol)
        return feature

    def _news_score(self, symbol: str) -> float:
        base_asset = symbol.replace("USDT", "")
        related = [item["score"] for item in self.collector.news_entries if base_asset.lower() in item["title"].lower() or "crypto" in item["title"].lower()]
        if not related:
            fallback_mode = self.config_manager.get()["analysis"].get("rss_non_english_fallback", "neutral")
            return 0.0 if fallback_mode == "neutral" else -0.1
        return clamp(mean(related), -1.0, 1.0)

    def _price_seconds_ago(self, symbol: str, seconds: int) -> float:
        now_ts = time.time()
        entries = list(self.collector.price_history.get(symbol, []))
        candidate = self.runtime_state.latest_prices.get(symbol, 0.0)
        for ts, price in reversed(entries):
            if now_ts - ts >= seconds:
                candidate = price
                break
        return candidate

    @staticmethod
    def _volatility_score(prices: list[float]) -> float:
        if len(prices) < 5:
            return 0.0
        returns = [pct_change(prices[i], prices[i - 1]) for i in range(1, len(prices))]
        vol = stddev(returns)
        return clamp(vol * 100, 0.0, 1.0)

    def _derive_label(self, symbol: str) -> int:
        recent = self.db.get_recent_candles(symbol, "1m", 12)
        if len(recent) < 7:
            return 0  # hold
        current = recent[-1]["close"]
        future = recent[min(len(recent) - 1, 6)]["close"]
        change = pct_change(future, current)
        if change > 0.008:
            return 1
        if change < -0.008:
            return 2
        return 0
