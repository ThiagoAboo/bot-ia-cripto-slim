from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .utils import hash_password, make_csv, password_matches


class WebUI:
    def __init__(self, config_manager, runtime_state, db, tracer, collector, analyzer, decision, executor, model_registry, event_bus):
        self.config_manager = config_manager
        self.runtime_state = runtime_state
        self.db = db
        self.tracer = tracer
        self.collector = collector
        self.analyzer = analyzer
        self.decision = decision
        self.executor = executor
        self.model_registry = model_registry
        self.event_bus = event_bus
        self.templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
        self.app = self._build_app()

    def _build_app(self) -> FastAPI:
        config = self.config_manager.get()
        app = FastAPI(title="bot-ia-cripto", version="2.0-slim")
        static_dir = Path(__file__).resolve().parent / "static"
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

        public_prefixes = ("/static", "/login", "/health", "/favicon.ico", "/.well-known", "/docs", "/openapi.json")

        @app.middleware("http")
        async def ensure_auth(request: Request, call_next):
            path = request.url.path
            if path.startswith(public_prefixes) or path.startswith("/ws"):
                return await call_next(request)
            if not request.session.get("authenticated"):
                return RedirectResponse("/login", status_code=303)
            return await call_next(request)

        @app.get("/health")
        async def health():
            return {"status": self.runtime_state.system_status, "running": self.runtime_state.running}

        @app.get("/login", response_class=HTMLResponse)
        async def login_page(request: Request):
            return self.templates.TemplateResponse(request, "login.html", {"error": None})

        @app.post("/login", response_class=HTMLResponse)
        async def login(request: Request, username: str = Form(...), password: str = Form(...)):
            auth = self.config_manager.get()["webui"]["authentication"]
            if username == auth["username"] and password_matches(password, auth["password_hash"]):
                request.session["authenticated"] = True
                return RedirectResponse("/", status_code=303)
            return self.templates.TemplateResponse(request, "login.html", {"error": "Usuário ou senha inválidos."})

        @app.post("/logout")
        async def logout(request: Request):
            request.session.clear()
            return RedirectResponse("/login", status_code=303)

        @app.get("/", response_class=HTMLResponse)
        async def dashboard(request: Request):
            context = self._base_context(request)
            context["page"] = "dashboard"
            return self.templates.TemplateResponse(request, "dashboard.html", context)

        @app.get("/config", response_class=HTMLResponse)
        async def config_page(request: Request):
            context = self._base_context(request)
            context["page"] = "config"
            context["config_yaml"] = Path(self.config_manager.config_path).read_text(encoding="utf-8")
            context["symbols_yaml"] = Path(self.config_manager.symbols_path).read_text(encoding="utf-8")
            return self.templates.TemplateResponse(request, "config.html", context)

        @app.get("/traces", response_class=HTMLResponse)
        async def traces_page(request: Request):
            context = self._base_context(request)
            context["page"] = "traces"
            return self.templates.TemplateResponse(request, "traces.html", context)

        @app.get("/training", response_class=HTMLResponse)
        async def training_page(request: Request):
            context = self._base_context(request)
            context["page"] = "training"
            context["models"] = self.model_registry.list_models()
            return self.templates.TemplateResponse(request, "training.html", context)

        @app.get("/api/dashboard")
        async def api_dashboard():
            balances = self.db.list_balances()
            portfolio_value = self.executor.portfolio_value()
            usdt = next((b for b in balances if b["asset"] == "USDT"), {"free": 0.0})
            orders = self.db.recent_orders(20)
            positions = self.executor.open_positions()
            pnl_total = portfolio_value - float(self.config_manager.get()["risk"]["simulated_initial_balance"])
            return {
                "runtime": self.runtime_state.snapshot(),
                "balances": balances,
                "positions": positions,
                "orders": orders,
                "portfolio_value": portfolio_value,
                "free_usdt": usdt["free"],
                "pnl_total": pnl_total,
                "models": self.model_registry.list_models(),
            }

        @app.post("/api/system/action")
        async def api_system_action(payload: dict[str, Any]):
            action = payload.get("action")
            if action == "start":
                self.runtime_state.running = True
                self.runtime_state.system_status = "active"
            elif action == "stop":
                self.runtime_state.running = False
                self.runtime_state.system_status = "stopped"
            elif action == "start_simulated":
                cfg = self.config_manager.get()
                cfg["general"]["trade_mode"] = "simulated"
                self.config_manager.save_config(cfg)
                self.runtime_state.mode = "simulated"
            elif action == "start_real":
                cfg = self.config_manager.get()
                cfg["general"]["trade_mode"] = "real"
                self.config_manager.save_config(cfg)
                self.runtime_state.mode = "real"
            elif action == "reset_wallet":
                self.db.reset_simulated_wallet(self.config_manager.get()["risk"]["simulated_initial_balance"])
            elif action == "reload_config":
                self.config_manager.reload()
                self.runtime_state.mode = self.config_manager.get()["general"]["trade_mode"]
                self.runtime_state.active_symbols = set(self.config_manager.get_symbols().get("base_symbols", []))
            elif action == "refresh_market":
                self.collector.force_refresh_market()
            elif action == "refresh_social":
                self.collector.force_refresh_social()
            else:
                return JSONResponse({"ok": False, "error": "Ação inválida"}, status_code=400)
            self.tracer.trace("webui", "system", message="Ação do painel executada", data={"action": action})
            return {"ok": True}

        @app.post("/api/config/save")
        async def api_save_config(payload: dict[str, Any]):
            import yaml

            config_yaml = payload.get("config_yaml", "")
            symbols_yaml = payload.get("symbols_yaml", "")

            try:
                yaml.safe_load(config_yaml)
                yaml.safe_load(symbols_yaml)
            except yaml.YAMLError as exc:
                mark = getattr(exc, "problem_mark", None)
                if mark is not None:
                    message = f"YAML inválido na linha {mark.line + 1}, coluna {mark.column + 1}: {exc}"
                else:
                    message = f"YAML inválido: {exc}"
                return JSONResponse({"ok": False, "error": message}, status_code=400)

            self.config_manager.save_config_text(config_yaml)
            self.config_manager.save_symbols_text(symbols_yaml)
            self.config_manager.reload()
            self.runtime_state.mode = self.config_manager.get()["general"]["trade_mode"]
            self.runtime_state.active_symbols = set(self.config_manager.get_symbols().get("base_symbols", []))
            self.tracer.trace("webui", "config_reload", message="Configuração salva via painel")
            return {"ok": True}

        @app.post("/api/auth/password")
        async def api_change_password(payload: dict[str, Any]):
            cfg = self.config_manager.get()
            cfg["webui"]["authentication"]["password_hash"] = hash_password(payload["new_password"])
            self.config_manager.save_config(cfg)
            self.tracer.trace("webui", "system", message="Senha da interface alterada")
            return {"ok": True}

        @app.get("/api/traces")
        async def api_traces(limit: int = 200, symbol: str | None = None, level: str | None = None):
            return {"rows": self.db.recent_traces(limit=limit, symbol=symbol, level=level)}

        @app.get("/api/traces/export")
        async def api_export_traces(format: str = "json"):
            rows = self.db.recent_traces(limit=2000)
            if format == "csv":
                csv_bytes = make_csv(
                    [
                        {
                            "timestamp": row["timestamp"],
                            "component": row["component"],
                            "event_type": row["event_type"],
                            "symbol": row.get("symbol"),
                            "level": row["level"],
                            "message": row.get("message"),
                            "data": json.dumps(row.get("data", {}), ensure_ascii=False),
                        }
                        for row in rows
                    ]
                )
                return Response(content=csv_bytes, media_type="text/csv", headers={"Content-Disposition": "attachment; filename=traces.csv"})
            return JSONResponse(rows)

        @app.get("/api/models")
        async def api_models():
            return {"models": self.model_registry.list_models()}

        @app.post("/api/models/train")
        async def api_train_model(payload: dict[str, Any]):
            model_name = payload.get("model_name") or "Novo Modelo"
            model_type = payload.get("model_type") or "random_forest"
            self.tracer.trace("training", "training", message="Treinamento iniciado", data={"model_name": model_name, "model_type": model_type})
            rows = self.db.latest_feature_rows(limit=10000)
            config = self.config_manager.get()["ai_model"]["training"]
            metadata = self.model_registry.train_model(
                model_name=model_name,
                model_type=model_type,
                training_rows=rows,
                random_state=int(config["random_state"]),
                test_size=float(config["test_size"]),
            )
            self.tracer.trace("training", "training", message="Treinamento concluído", data={"model_id": metadata["id"], "metrics": metadata["metrics"]})
            return {"ok": True, "model": metadata}

        @app.post("/api/models/activate")
        async def api_activate_model(payload: dict[str, Any]):
            model_id = payload["model_id"]
            self.model_registry.set_active(model_id)
            cfg = self.config_manager.get()
            cfg["ai_model"]["active_model_id"] = model_id
            self.config_manager.save_config(cfg)
            self.runtime_state.active_model_id = model_id
            self.tracer.trace("training", "model_switched", message="Modelo ativo alterado", data={"model_id": model_id})
            return {"ok": True}

        @app.post("/api/models/delete")
        async def api_delete_model(payload: dict[str, Any]):
            self.model_registry.delete(payload["model_id"])
            self.tracer.trace("training", "system", message="Modelo excluído", data={"model_id": payload["model_id"]})
            return {"ok": True}

        @app.websocket("/ws/logs")
        async def ws_logs(websocket: WebSocket):
            await websocket.accept()
            try:
                recent = self.event_bus.recent(50)
                await websocket.send_json({"type": "bootstrap", "rows": recent})
                last_trace_id = recent[-1]["trace_id"] if recent else None
                while True:
                    await asyncio.sleep(1)
                    current = self.event_bus.recent(100)
                    current_last = current[-1]["trace_id"] if current else None
                    if current_last != last_trace_id:
                        await websocket.send_json({"type": "append", "rows": current})
                        last_trace_id = current_last
            except WebSocketDisconnect:
                return

        app.add_middleware(
            SessionMiddleware,
            secret_key=config["webui"]["session_secret"],
            same_site="lax",
            https_only=False,
            session_cookie="bot_ia_cripto_session",
        )

        return app

    def _base_context(self, request: Request) -> dict[str, Any]:
        return {
            "request": request,
            "runtime": self.runtime_state.snapshot(),
            "app_name": "bot-ia-cripto",
        }
