from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from typing import Any

import feedparser
import requests
import websockets

from .utils import iso_now, pct_change, safe_float


TIMEFRAME_TO_INTERVAL = {
    "1m": "1m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "4h": "4h",
    "12h": "12h",
    "24h": "1d",
    "1w": "1w",
    "1M": "1M",
}


class CollectorService:
    def __init__(self, db, config_manager, runtime_state, tracer):
        self.db = db
        self.config_manager = config_manager
        self.runtime_state = runtime_state
        self.tracer = tracer
        self.stop_event = threading.Event()
        self.market_thread = threading.Thread(target=self.market_loop, name="collector-market", daemon=True)
        self.social_thread = threading.Thread(target=self.social_loop, name="collector-social", daemon=True)
        self.rss_thread = threading.Thread(target=self.rss_loop, name="collector-rss", daemon=True)
        self.ws_thread = threading.Thread(target=self.websocket_loop, name="collector-ws", daemon=True)

        self.price_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=3600))
        self.book_snapshots: dict[str, deque] = defaultdict(lambda: deque(maxlen=300))
        self.news_entries: list[dict[str, Any]] = []

    def start(self) -> None:
        self.market_thread.start()
        self.social_thread.start()
        self.rss_thread.start()
        self.ws_thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        for thread in (self.market_thread, self.social_thread, self.rss_thread, self.ws_thread):
            thread.join(timeout=5)

    def active_symbols(self) -> list[str]:
        return sorted(self.runtime_state.active_symbols)

    def market_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.fetch_market_snapshot()
                self.runtime_state.last_market_update = iso_now()
                self.runtime_state.system_status = "active"
            except Exception as exc:
                self.runtime_state.last_error = str(exc)
                self.runtime_state.system_status = "error"
                self.tracer.trace("collector", "error", level="ERROR", message="Erro na coleta de mercado", data={"error": str(exc)})
            interval = self.config_manager.get()["general"]["data_fetch_interval_seconds"]
            self.stop_event.wait(interval)

    def fetch_market_snapshot(self) -> None:
        config = self.config_manager.get()
        base_url = config["binance"]["rest_base_url"]
        symbols = self.active_symbols()
        if not symbols:
            return

        for symbol in symbols:
            symbol_l = symbol.lower()
            ticker = requests.get(f"{base_url}/api/v3/ticker/24hr", params={"symbol": symbol}, timeout=10).json()
            book = requests.get(f"{base_url}/api/v3/depth", params={"symbol": symbol, "limit": 10}, timeout=10).json()
            price = safe_float(ticker.get("lastPrice"))
            volume = safe_float(ticker.get("volume"))
            bid_liquidity = sum(safe_float(x[1]) for x in book.get("bids", []))
            ask_liquidity = sum(safe_float(x[1]) for x in book.get("asks", []))
            spread = 0.0
            if book.get("bids") and book.get("asks"):
                best_bid = safe_float(book["bids"][0][0])
                best_ask = safe_float(book["asks"][0][0])
                if best_bid:
                    spread = (best_ask - best_bid) / best_bid

            snapshot = {
                "price": price,
                "volume_24h": volume,
                "price_change_percent": safe_float(ticker.get("priceChangePercent")) / 100.0,
                "quote_volume": safe_float(ticker.get("quoteVolume")),
                "bid_liquidity": bid_liquidity,
                "ask_liquidity": ask_liquidity,
                "spread": spread,
                "book": book,
                "updated_at": iso_now(),
            }
            self.runtime_state.latest_prices[symbol] = price
            self.runtime_state.latest_book[symbol] = snapshot
            self.price_history[symbol].append((time.time(), price))
            self.book_snapshots[symbol].append(snapshot)
            self.tracer.trace("collector", "collect", symbol=symbol, message="Snapshot de mercado atualizado", data=snapshot)

            for timeframe in config["analysis"]["momentum_resolutions"]:
                interval = TIMEFRAME_TO_INTERVAL.get(timeframe)
                if not interval:
                    continue
                self.fetch_candles(symbol=symbol, interval=interval, limit=min(500, config["ai_model"]["training"]["candle_limit"]))

    def fetch_candles(self, symbol: str, interval: str, limit: int = 300) -> None:
        config = self.config_manager.get()
        base_url = config["binance"]["rest_base_url"]
        response = requests.get(
            f"{base_url}/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=15,
        )
        response.raise_for_status()
        rows = response.json()
        app_tf = next((k for k, v in TIMEFRAME_TO_INTERVAL.items() if v == interval), interval)
        for row in rows:
            candle = {
                "symbol": symbol,
                "timeframe": app_tf,
                "open_time": datetime.fromtimestamp(row[0] / 1000, tz=UTC).isoformat(),
                "close_time": datetime.fromtimestamp(row[6] / 1000, tz=UTC).isoformat(),
                "open": safe_float(row[1]),
                "high": safe_float(row[2]),
                "low": safe_float(row[3]),
                "close": safe_float(row[4]),
                "volume": safe_float(row[5]),
                "source": "binance_rest",
            }
            self.db.upsert_candle(candle)

    async def _websocket_consumer(self) -> None:
        config = self.config_manager.get()
        symbols = self.active_symbols()
        if not symbols:
            return
        streams = []
        for symbol in symbols:
            streams.append(f"{symbol.lower()}@bookTicker")
            streams.append(f"{symbol.lower()}@aggTrade")
        url = f"{config['binance']['ws_base_url']}?streams={'/'.join(streams)}"
        async with websockets.connect(url, ping_interval=20, ping_timeout=20, close_timeout=5) as websocket:
            while not self.stop_event.is_set():
                message = await asyncio.wait_for(websocket.recv(), timeout=30)
                payload = json.loads(message)
                stream = payload.get("stream", "")
                data = payload.get("data", {})
                symbol = data.get("s")
                if not symbol:
                    continue
                if stream.endswith("@bookTicker"):
                    book = self.runtime_state.latest_book.get(symbol, {})
                    book["best_bid"] = safe_float(data.get("b"))
                    book["best_ask"] = safe_float(data.get("a"))
                    if book.get("best_bid"):
                        book["spread"] = max(0.0, (book["best_ask"] - book["best_bid"]) / book["best_bid"])
                    self.runtime_state.latest_book[symbol] = book
                elif stream.endswith("@aggTrade"):
                    price = safe_float(data.get("p"))
                    self.runtime_state.latest_prices[symbol] = price
                    self.price_history[symbol].append((time.time(), price))

    def websocket_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                asyncio.run(self._websocket_consumer())
            except Exception as exc:
                self.tracer.trace("collector", "warning", level="WARNING", message="WebSocket indisponível, seguindo com REST", data={"error": str(exc)})
                self.stop_event.wait(15)

    def social_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.fetch_social_data()
                self.runtime_state.last_social_update = iso_now()
            except Exception as exc:
                self.tracer.trace("collector", "error", level="ERROR", message="Erro na coleta social", data={"error": str(exc)})
            hours = self.config_manager.get()["social"]["update_interval_hours"]
            self.stop_event.wait(max(60, hours * 3600))

    def fetch_social_data(self) -> None:
        config = self.config_manager.get()
        endpoint = config["social"]["endpoint"]
        response = requests.get(endpoint, timeout=20, headers={"User-Agent": "bot-ia-cripto/1.0"})
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("results") or payload.get("data") or []
        social_scores: dict[str, float] = {}
        for item in rows[:150]:
            ticker = (item.get("ticker") or item.get("symbol") or "").upper()
            if not ticker:
                continue
            symbol = ticker if ticker.endswith("USDT") else f"{ticker}USDT"
            score = safe_float(item.get("rank", 0))
            mentions = safe_float(item.get("mentions", 0))
            normalized = min(1.0, (mentions / 100.0) + max(0.0, 1 - (score / 100.0)))
            social_scores[symbol] = round(normalized / 2.0, 4)

        self.runtime_state.latest_social_scores = social_scores
        strong_threshold = config["social"]["strong_threshold"]
        keep_minutes = config["social"]["strong_keep_minutes"]
        now = datetime.now(UTC)
        for symbol, score in social_scores.items():
            if score >= strong_threshold:
                self.runtime_state.strong_symbols[symbol] = (now + timedelta(minutes=keep_minutes)).isoformat()
                self.tracer.trace("collector", "social_strong", symbol=symbol, message="Moeda forte via social", data={"score": score})

    def rss_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.fetch_rss()
                self.runtime_state.last_rss_update = iso_now()
            except Exception as exc:
                self.tracer.trace("collector", "warning", level="WARNING", message="Erro na coleta RSS", data={"error": str(exc)})
            self.stop_event.wait(1800)

    def fetch_rss(self) -> None:
        config = self.config_manager.get()
        feed = feedparser.parse(config["analysis"]["rss_feed_url"])
        entries = []
        max_entries = int(config["analysis"].get("rss_max_entries", 30))
        for item in feed.entries[:max_entries]:
            title = getattr(item, "title", "") or ""
            summary = getattr(item, "summary", "") or ""
            text = f"{title} {summary}".lower()
            entries.append(
                {
                    "title": title,
                    "summary": summary,
                    "link": getattr(item, "link", ""),
                    "published": getattr(item, "published", iso_now()),
                    "score": self.simple_news_score(text),
                }
            )
        self.news_entries = entries
        self.tracer.trace("collector", "collect", message="Feed RSS atualizado", data={"entries": len(entries)})

    @staticmethod
    def simple_news_score(text: str) -> float:
        positive = ["surge", "adoption", "approve", "bull", "growth", "partnership", "record", "up"]
        negative = ["hack", "ban", "lawsuit", "bear", "drop", "down", "exploit", "fraud"]
        score = 0
        for word in positive:
            if word in text:
                score += 1
        for word in negative:
            if word in text:
                score -= 1
        return max(-1.0, min(1.0, score / 3.0))

    def force_refresh_market(self) -> None:
        self.fetch_market_snapshot()

    def force_refresh_social(self) -> None:
        self.fetch_social_data()
