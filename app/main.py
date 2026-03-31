from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from .analyzer import AnalyzerService
from .collector import CollectorService
from .db import Database
from .decision import DecisionService
from .executor import ExecutorService
from .models import ModelRegistry
from .tracer import TracerService
from .utils import ConfigManager, EventBus, RuntimeState
from .webui import WebUI


def project_root() -> Path:
    env_root = os.getenv("APP_BASE_DIR")
    if env_root:
        return Path(env_root).resolve()
    return Path(__file__).resolve().parent.parent




def load_local_env(root: Path) -> None:
    env_path = root / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def resolve_paths() -> tuple[Path, Path]:
    root = project_root()
    return root / "config" / "bot_config.yaml", root / "config" / "symbols.yaml"


def build_services():
    root = project_root()
    os.environ.setdefault("APP_BASE_DIR", str(root))
    load_local_env(root)
    os.environ.setdefault("APP_BASE_DIR", str(root))
    config_path, symbols_path = resolve_paths()
    config_manager = ConfigManager(config_path, symbols_path)
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
    analyzer = AnalyzerService(
        db=db,
        config_manager=config_manager,
        runtime_state=runtime_state,
        tracer=tracer,
        collector=collector,
    )
    decision = DecisionService(
        config_manager=config_manager,
        runtime_state=runtime_state,
        tracer=tracer,
        model_registry=model_registry,
        executor=executor,
    )

    services = {
        "config_manager": config_manager,
        "db": db,
        "runtime_state": runtime_state,
        "event_bus": event_bus,
        "tracer": tracer,
        "model_registry": model_registry,
        "executor": executor,
        "collector": collector,
        "analyzer": analyzer,
        "decision": decision,
    }
    return services


def create_application() -> tuple[FastAPI, ConfigManager]:
    services = build_services()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        services["tracer"].start()
        services["executor"].start()
        services["collector"].start()
        services["analyzer"].start()
        services["decision"].start()
        services["runtime_state"].system_status = "active"
        try:
            yield
        finally:
            services["runtime_state"].system_status = "stopping"
            services["decision"].stop()
            services["analyzer"].stop()
            services["collector"].stop()
            services["executor"].stop()
            services["tracer"].stop()
            services["db"].close()

    webui = WebUI(
        config_manager=services["config_manager"],
        runtime_state=services["runtime_state"],
        db=services["db"],
        tracer=services["tracer"],
        collector=services["collector"],
        analyzer=services["analyzer"],
        decision=services["decision"],
        executor=services["executor"],
        model_registry=services["model_registry"],
        event_bus=services["event_bus"],
    )

    base_app = webui.app
    app = FastAPI(title=base_app.title, version=base_app.version, lifespan=lifespan)
    app.mount("/", base_app)
    return app, services["config_manager"]


def main() -> None:
    app, config_manager = create_application()
    webui_config = config_manager.get()["webui"]
    uvicorn.run(app, host=webui_config["host"], port=int(webui_config["port"]))


if __name__ == "__main__":
    main()
