from __future__ import annotations

import csv
import io
import json
import math
import os
import threading
import time
import uuid
from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import bcrypt
import yaml


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utcnow().isoformat()


def ensure_dir(path: str | Path) -> Path:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    return value


class ConfigManager:
    def __init__(self, config_path: str | Path, symbols_path: str | Path):
        self.config_path = Path(config_path)
        self.symbols_path = Path(symbols_path)
        self._lock = threading.RLock()
        self._config: dict[str, Any] = {}
        self._symbols: dict[str, Any] = {}
        self.reload()

    def reload(self) -> None:
        with self._lock:
            self._config = expand_env(yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {})
            self._symbols = yaml.safe_load(self.symbols_path.read_text(encoding="utf-8")) or {}

    def get(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._config)

    def get_symbols(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._symbols)

    def save_config(self, data: dict[str, Any]) -> None:
        with self._lock:
            self._config = data
            self.config_path.write_text(
                yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )

    def save_symbols(self, data: dict[str, Any]) -> None:
        with self._lock:
            self._symbols = data
            self.symbols_path.write_text(
                yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )


def password_matches(plain_text: str, hashed: str) -> bool:
    if hashed.startswith("plain:"):
        return plain_text == hashed.split("plain:", 1)[1]
    return bcrypt.checkpw(plain_text.encode("utf-8"), hashed.encode("utf-8"))


def hash_password(plain_text: str) -> str:
    try:
        return bcrypt.hashpw(plain_text.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    except Exception:
        return f"plain:{plain_text}"


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def pct_change(current: float, previous: float) -> float:
    if not previous:
        return 0.0
    return (current - previous) / previous


def mean(values: Iterable[float]) -> float:
    data = list(values)
    return sum(data) / len(data) if data else 0.0


def stddev(values: Iterable[float]) -> float:
    data = list(values)
    if len(data) < 2:
        return 0.0
    avg = mean(data)
    variance = sum((x - avg) ** 2 for x in data) / len(data)
    return math.sqrt(variance)


def z_score(current: float, values: Iterable[float]) -> float:
    data = list(values)
    if not data:
        return 0.0
    avg = mean(data)
    std = stddev(data)
    if std == 0:
        return 0.0
    return (current - avg) / std


def slugify(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-")


def now_minus(days: int) -> str:
    return (utcnow() - timedelta(days=days)).isoformat()


def to_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def from_json(data: str | bytes | None, default: Any = None) -> Any:
    if not data:
        return default
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return default


def make_csv(rows: list[dict[str, Any]]) -> bytes:
    if not rows:
        rows = [{"message": "sem dados"}]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _stable_snapshot_copy(value: Any, *, retries: int = 5, delay: float = 0.002) -> Any:
    for attempt in range(retries):
        try:
            if isinstance(value, deque):
                return [_stable_snapshot_copy(item, retries=1, delay=delay) for item in list(value)]
            if isinstance(value, dict):
                return {key: _stable_snapshot_copy(item, retries=1, delay=delay) for key, item in list(value.items())}
            if isinstance(value, set):
                return sorted(_stable_snapshot_copy(item, retries=1, delay=delay) for item in list(value))
            if isinstance(value, list):
                return [_stable_snapshot_copy(item, retries=1, delay=delay) for item in list(value)]
            if isinstance(value, tuple):
                return [_stable_snapshot_copy(item, retries=1, delay=delay) for item in list(value)]
            return deepcopy(value)
        except RuntimeError:
            if attempt == retries - 1:
                break
            time.sleep(delay)

    if isinstance(value, dict):
        return {}
    if isinstance(value, (list, tuple, deque, set)):
        return []
    try:
        return deepcopy(value)
    except Exception:
        return None


@dataclass
class RuntimeState:
    running: bool = True
    mode: str = "simulated"
    last_market_update: str | None = None
    last_social_update: str | None = None
    last_rss_update: str | None = None
    last_feature_update: str | None = None
    last_inference_update: str | None = None
    last_order_update: str | None = None
    system_status: str = "starting"
    active_model_id: str = "bootstrap-rf"
    active_symbols: set[str] = field(default_factory=set)
    strong_symbols: dict[str, str] = field(default_factory=dict)
    latest_prices: dict[str, float] = field(default_factory=dict)
    latest_book: dict[str, dict[str, Any]] = field(default_factory=dict)
    latest_features: dict[str, dict[str, Any]] = field(default_factory=dict)
    latest_predictions: dict[str, dict[str, Any]] = field(default_factory=dict)
    latest_social_scores: dict[str, float] = field(default_factory=dict)
    latest_rss_scores: dict[str, float] = field(default_factory=dict)
    log_buffer: deque = field(default_factory=lambda: deque(maxlen=400))
    training_jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "mode": self.mode,
            "last_market_update": self.last_market_update,
            "last_social_update": self.last_social_update,
            "last_rss_update": self.last_rss_update,
            "last_feature_update": self.last_feature_update,
            "last_inference_update": self.last_inference_update,
            "last_order_update": self.last_order_update,
            "system_status": self.system_status,
            "active_model_id": self.active_model_id,
            "active_symbols": _stable_snapshot_copy(self.active_symbols),
            "strong_symbols": _stable_snapshot_copy(self.strong_symbols),
            "latest_prices": _stable_snapshot_copy(self.latest_prices),
            "latest_book": _stable_snapshot_copy(self.latest_book),
            "latest_features": _stable_snapshot_copy(self.latest_features),
            "latest_predictions": _stable_snapshot_copy(self.latest_predictions),
            "latest_social_scores": _stable_snapshot_copy(self.latest_social_scores),
            "latest_rss_scores": _stable_snapshot_copy(self.latest_rss_scores),
            "log_buffer": _stable_snapshot_copy(self.log_buffer),
            "training_jobs": _stable_snapshot_copy(self.training_jobs),
            "last_error": self.last_error,
        }


class EventBus:
    def __init__(self, maxlen: int = 400):
        self._lock = threading.Lock()
        self._events: deque[dict[str, Any]] = deque(maxlen=maxlen)

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._events.append(event)

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events)[-limit:]


def build_trace(
    component: str,
    event_type: str,
    symbol: str | None = None,
    level: str = "INFO",
    message: str | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "trace_id": str(uuid.uuid4()),
        "timestamp": iso_now(),
        "component": component,
        "event_type": event_type,
        "symbol": symbol,
        "level": level,
        "message": message or event_type,
        "data": data or {},
    }
