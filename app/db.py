from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

from .utils import ensure_dir, from_json, to_json


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        ensure_dir(Path(db_path).parent)
        self._local = threading.local()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            self._local.conn = conn
        return conn

    def _init_db(self) -> None:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        cur = conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS traces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id TEXT UNIQUE NOT NULL,
                timestamp TEXT NOT NULL,
                component TEXT NOT NULL,
                event_type TEXT NOT NULL,
                symbol TEXT,
                level TEXT NOT NULL,
                message TEXT,
                data_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS candles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                open_time TEXT NOT NULL,
                close_time TEXT,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL,
                source TEXT DEFAULT 'binance_rest',
                UNIQUE(symbol, timeframe, open_time)
            );

            CREATE INDEX IF NOT EXISTS idx_candles_symbol_tf_time ON candles(symbol, timeframe, open_time DESC);

            CREATE TABLE IF NOT EXISTS features (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                data_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_features_symbol_time ON features(symbol, timestamp DESC);

            CREATE TABLE IF NOT EXISTS models_metadata (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                model_type TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metrics_json TEXT,
                feature_order_json TEXT,
                model_path TEXT NOT NULL,
                scaler_path TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS simulated_balance (
                asset TEXT PRIMARY KEY,
                free REAL NOT NULL,
                locked REAL NOT NULL DEFAULT 0,
                avg_price REAL NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS simulated_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity REAL NOT NULL,
                price REAL NOT NULL,
                fee_asset TEXT,
                fee_amount REAL DEFAULT 0,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                confidence REAL DEFAULT 0,
                reason TEXT,
                pnl_usdt REAL DEFAULT 0,
                metadata_json TEXT
            );
            """
        )
        conn.commit()
        conn.close()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
        return cur

    def executescript(self, sql: str) -> None:
        conn = self._connect()
        conn.executescript(sql)
        conn.commit()

    def query_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        cur = self._connect().execute(sql, params)
        rows = cur.fetchall()
        return [dict(row) for row in rows]

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        cur = self._connect().execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None

    def insert_trace(self, trace: dict[str, Any]) -> None:
        self.execute(
            """
            INSERT OR IGNORE INTO traces
            (trace_id, timestamp, component, event_type, symbol, level, message, data_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trace["trace_id"],
                trace["timestamp"],
                trace["component"],
                trace["event_type"],
                trace.get("symbol"),
                trace["level"],
                trace.get("message"),
                to_json(trace.get("data", {})),
            ),
        )

    def insert_feature(self, timestamp: str, symbol: str, data: dict[str, Any]) -> None:
        self.execute(
            "INSERT INTO features (timestamp, symbol, data_json) VALUES (?, ?, ?)",
            (timestamp, symbol, to_json(data)),
        )

    def upsert_candle(self, candle: dict[str, Any]) -> None:
        self.execute(
            """
            INSERT INTO candles
            (symbol, timeframe, open_time, close_time, open, high, low, close, volume, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, timeframe, open_time) DO UPDATE SET
                close_time=excluded.close_time,
                open=excluded.open,
                high=excluded.high,
                low=excluded.low,
                close=excluded.close,
                volume=excluded.volume,
                source=excluded.source
            """,
            (
                candle["symbol"],
                candle["timeframe"],
                candle["open_time"],
                candle.get("close_time"),
                candle.get("open"),
                candle.get("high"),
                candle.get("low"),
                candle.get("close"),
                candle.get("volume"),
                candle.get("source", "unknown"),
            ),
        )

    def get_recent_candles(self, symbol: str, timeframe: str, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.query_all(
            """
            SELECT * FROM candles
            WHERE symbol = ? AND timeframe = ?
            ORDER BY open_time DESC
            LIMIT ?
            """,
            (symbol, timeframe, limit),
        )
        rows.reverse()
        return rows

    def save_model_metadata(self, metadata: dict[str, Any]) -> None:
        self.execute(
            """
            INSERT INTO models_metadata
            (id, name, model_type, is_active, created_at, updated_at, metrics_json, feature_order_json, model_path, scaler_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                model_type=excluded.model_type,
                is_active=excluded.is_active,
                updated_at=excluded.updated_at,
                metrics_json=excluded.metrics_json,
                feature_order_json=excluded.feature_order_json,
                model_path=excluded.model_path,
                scaler_path=excluded.scaler_path
            """,
            (
                metadata["id"],
                metadata["name"],
                metadata["model_type"],
                1 if metadata.get("is_active") else 0,
                metadata["created_at"],
                metadata["updated_at"],
                to_json(metadata.get("metrics", {})),
                to_json(metadata.get("feature_order", [])),
                metadata["model_path"],
                metadata["scaler_path"],
            ),
        )

    def list_models(self) -> list[dict[str, Any]]:
        rows = self.query_all("SELECT * FROM models_metadata ORDER BY updated_at DESC")
        for row in rows:
            row["metrics"] = from_json(row.get("metrics_json"), {})
            row["feature_order"] = from_json(row.get("feature_order_json"), [])
            row["is_active"] = bool(row["is_active"])
        return rows

    def set_active_model(self, model_id: str) -> None:
        self.execute("UPDATE models_metadata SET is_active = 0")
        self.execute("UPDATE models_metadata SET is_active = 1 WHERE id = ?", (model_id,))

    def get_active_model(self) -> dict[str, Any] | None:
        row = self.query_one("SELECT * FROM models_metadata WHERE is_active = 1 LIMIT 1")
        if row:
            row["metrics"] = from_json(row.get("metrics_json"), {})
            row["feature_order"] = from_json(row.get("feature_order_json"), [])
            row["is_active"] = bool(row["is_active"])
        return row

    def reset_simulated_wallet(self, initial_balance: float) -> None:
        self.executescript(
            """
            DELETE FROM simulated_orders;
            DELETE FROM simulated_balance;
            """
        )
        self.execute(
            "INSERT INTO simulated_balance (asset, free, locked, avg_price) VALUES (?, ?, 0, 1)",
            ("USDT", float(initial_balance)),
        )
        self.execute(
            "INSERT INTO simulated_balance (asset, free, locked, avg_price) VALUES (?, ?, 0, 0)",
            ("BNB", 0.0),
        )

    def get_balance(self, asset: str) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM simulated_balance WHERE asset = ?", (asset,))

    def upsert_balance(self, asset: str, free: float, locked: float = 0.0, avg_price: float = 0.0) -> None:
        self.execute(
            """
            INSERT INTO simulated_balance (asset, free, locked, avg_price)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(asset) DO UPDATE SET
                free=excluded.free,
                locked=excluded.locked,
                avg_price=excluded.avg_price
            """,
            (asset, float(free), float(locked), float(avg_price)),
        )

    def list_balances(self) -> list[dict[str, Any]]:
        return self.query_all("SELECT * FROM simulated_balance ORDER BY asset")

    def insert_order(self, order: dict[str, Any]) -> int:
        cur = self.execute(
            """
            INSERT INTO simulated_orders
            (created_at, symbol, side, quantity, price, fee_asset, fee_amount, mode, status, confidence, reason, pnl_usdt, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order["created_at"],
                order["symbol"],
                order["side"],
                order["quantity"],
                order["price"],
                order.get("fee_asset"),
                order.get("fee_amount", 0),
                order["mode"],
                order["status"],
                order.get("confidence", 0),
                order.get("reason"),
                order.get("pnl_usdt", 0),
                to_json(order.get("metadata", {})),
            ),
        )
        return int(cur.lastrowid)

    def recent_orders(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.query_all(
            "SELECT * FROM simulated_orders ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        for row in rows:
            row["metadata"] = from_json(row.get("metadata_json"), {})
        return rows

    def recent_traces(self, limit: int = 200, symbol: str | None = None, level: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM traces WHERE 1=1"
        params: list[Any] = []
        if symbol:
            sql += " AND symbol = ?"
            params.append(symbol)
        if level:
            sql += " AND level = ?"
            params.append(level)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self.query_all(sql, tuple(params))
        for row in rows:
            row["data"] = from_json(row.get("data_json"), {})
        return rows

    def cleanup_traces(self, older_than_iso: str) -> int:
        cur = self.execute("DELETE FROM traces WHERE timestamp < ?", (older_than_iso,))
        return cur.rowcount

    def latest_feature_rows(self, limit: int = 5000) -> list[dict[str, Any]]:
        rows = self.query_all(
            """
            SELECT * FROM features
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (limit,),
        )
        for row in rows:
            row["data"] = from_json(row.get("data_json"), {})
        return rows

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
