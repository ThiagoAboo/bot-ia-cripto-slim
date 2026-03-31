from __future__ import annotations

import queue
import threading

from .utils import clamp, iso_now


class DecisionService:
    def __init__(self, config_manager, runtime_state, tracer, model_registry, executor):
        self.config_manager = config_manager
        self.runtime_state = runtime_state
        self.tracer = tracer
        self.model_registry = model_registry
        self.executor = executor
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name="decision", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=5)

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.evaluate_all()
            except Exception as exc:
                self.tracer.trace("decision", "error", level="ERROR", message="Erro na decisão", data={"error": str(exc)})
            self.stop_event.wait(5)

    def evaluate_all(self) -> None:
        config = self.config_manager.get()
        for symbol in sorted(self.runtime_state.active_symbols):
            feature = self.runtime_state.latest_features.get(symbol)
            if not feature:
                continue

            result = self.model_registry.predict(feature)
            confidence = float(result["confidence"])
            if symbol in self.runtime_state.strong_symbols:
                confidence = clamp(confidence * float(config["social"]["boost_factor"]), 0.0, 1.0)

            result["confidence"] = confidence
            self.runtime_state.latest_predictions[symbol] = result
            self.runtime_state.last_inference_update = iso_now()

            self.tracer.trace(
                "decision",
                "inference",
                symbol=symbol,
                message="Inferência concluída",
                data={
                    "input_features": {k: feature.get(k) for k in feature.keys() if k not in {"timestamp", "symbol"}},
                    "output_probabilities": result["probabilities"],
                    "confidence": confidence,
                    "action": result["action"],
                    "model_id": result.get("model_id"),
                },
            )

            if confidence < float(config["ai_model"]["min_confidence"]):
                self.tracer.trace("decision", "rule_filter", symbol=symbol, message="Sinal filtrado por confiança", data={"confidence": confidence})
                continue

            if feature["liquidity_score"] < float(config["risk"]["min_liquidity_score"]):
                self.tracer.trace("decision", "rule_filter", symbol=symbol, message="Sinal filtrado por liquidez", data={"liquidity_score": feature["liquidity_score"]})
                continue

            if result["action"] == "hold":
                continue

            order = {
                "created_at": iso_now(),
                "symbol": symbol,
                "side": result["action"],
                "confidence": confidence,
                "reason": "model_signal",
                "feature_snapshot": feature,
                "model_snapshot": result,
            }
            self.executor.enqueue(order)
