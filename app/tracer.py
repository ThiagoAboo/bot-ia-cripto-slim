from __future__ import annotations

import queue
import threading
import time
from datetime import timedelta

from .utils import EventBus, build_trace, iso_now, now_minus


class TracerService:
    def __init__(self, db, runtime_state, config_manager, event_bus: EventBus):
        self.db = db
        self.runtime_state = runtime_state
        self.config_manager = config_manager
        self.event_bus = event_bus
        self.queue: queue.Queue = queue.Queue(maxsize=5000)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name="tracer", daemon=True)

    def start(self) -> None:
        self.thread.start()
        self.trace("tracer", "system", message="Tracer iniciado")

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=5)

    def trace(self, component: str, event_type: str, symbol: str | None = None, level: str = "INFO", message: str | None = None, data: dict | None = None) -> None:
        event = build_trace(component=component, event_type=event_type, symbol=symbol, level=level, message=message, data=data)
        try:
            self.queue.put_nowait(event)
        except queue.Full:
            fallback = build_trace(component="tracer", event_type="error", level="ERROR", message="Fila de traces cheia", data={"dropped_event": event_type})
            self.queue.put(fallback)

    def run(self) -> None:
        last_cleanup = 0.0
        while not self.stop_event.is_set():
            try:
                event = self.queue.get(timeout=1)
            except queue.Empty:
                event = None

            if event:
                self.db.insert_trace(event)
                self.runtime_state.log_buffer.append(event)
                self.event_bus.publish(event)

            now = time.time()
            if now - last_cleanup > 3600:
                retention_days = self.config_manager.get()["tracing"]["retention_days"]
                removed = self.db.cleanup_traces(now_minus(retention_days))
                if removed:
                    msg = build_trace("tracer", "system", message=f"Retenção aplicada, {removed} traces removidos", data={"removed": removed})
                    self.db.insert_trace(msg)
                    self.event_bus.publish(msg)
                last_cleanup = now
