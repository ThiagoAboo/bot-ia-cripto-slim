from __future__ import annotations

import threading
import time

import uvicorn

from .analyzer import AnalyzerService
from .collector import CollectorService
from .db import Database
from .decision import DecisionService
from .executor import ExecutorService
from .models import ModelRegistry
from .tracer import TracerService
from .utils import ConfigManager, EventBus, RuntimeState
from .webui import WebUI


def create_application():
    config_manager = ConfigManager("/app/config/bot_config.yaml", "/app/config/symbols.yaml")
    config = config_manager.get()
    db = Database(config["tracing"]["db_path"])
    runtime_state = RuntimeState(
        mode=config["general"]["trade_mode"],
        active_model_id=config["ai_model"].get("active_model_id", config["ai_model"]["default_model_name"]),
        active_symbols=set(config_manager.get_symbols().get("base_symbols", [])),
        system_status="starting",
    )
    event_bus = EventBus()

    tracer = TracerService(db=db, runtime_state=runtime_state, config_manager=config_manager, event_bus=event_bus)
    model_registry = ModelRegistry(db=db, config_manager=config_manager)
    model_registry.bootstrap_if_empty()
    active_model = db.get_active_model()
    if active_model:
        runtime_state.active_model_id = active_model["id"]

    executor = ExecutorService(db=db, config_manager=config_manager, runtime_state=runtime_state, tracer=tracer)
    collector = CollectorService(db=db, config_manager=config_manager, runtime_state=runtime_state, tracer=tracer)
    analyzer = AnalyzerService(db=db, config_manager=config_manager, runtime_state=runtime_state, tracer=tracer, collector=collector)
    decision = DecisionService(config_manager=config_manager, runtime_state=runtime_state, tracer=tracer, model_registry=model_registry, executor=executor)

    tracer.start()
    executor.start()
    collector.start()
    analyzer.start()
    decision.start()

    webui = WebUI(
        config_manager=config_manager,
        runtime_state=runtime_state,
        db=db,
        tracer=tracer,
        collector=collector,
        analyzer=analyzer,
        decision=decision,
        executor=executor,
        model_registry=model_registry,
        event_bus=event_bus,
    )

    @webui.app.on_event("shutdown")
    async def shutdown_event():
        runtime_state.system_status = "stopping"
        decision.stop()
        analyzer.stop()
        collector.stop()
        executor.stop()
        tracer.stop()
        db.close()

    return webui.app, config_manager


def main() -> None:
    app, config_manager = create_application()
    webui_config = config_manager.get()["webui"]
    uvicorn.run(app, host=webui_config["host"], port=int(webui_config["port"]))


if __name__ == "__main__":
    main()
