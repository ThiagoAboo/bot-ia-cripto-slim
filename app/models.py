from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.metrics import accuracy_score, confusion_matrix, f1_score, log_loss, precision_score, recall_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

try:
    from xgboost import XGBClassifier
except Exception:  # pragma: no cover
    XGBClassifier = None

from .utils import iso_now, slugify


FEATURE_ORDER = [
    "price",
    "value_zscore",
    "micro_momentum_score",
    "standard_momentum_score",
    "momentum_score",
    "social_score",
    "liquidity_score",
    "order_imbalance_score",
    "volume_anomaly_score",
    "news_sentiment_score",
    "spread_score",
    "volatility_score",
]


class ModelRegistry:
    def __init__(self, db, config_manager):
        self.db = db
        self.config_manager = config_manager
        self.loaded_cache: dict[str, tuple[Any, Pipeline]] = {}

    def bootstrap_if_empty(self) -> None:
        if self.db.list_models():
            return
        config = self.config_manager.get()
        models_path = Path(config["ai_model"]["models_path"])
        models_path.mkdir(parents=True, exist_ok=True)

        X = np.array(
            [
                [30000, -1.2, 0.8, 0.5, 0.62, 0.2, 0.6, 0.2, 0.4, 0.1, 0.6, 0.3],
                [31000, 1.1, -0.4, -0.5, -0.46, 0.1, 0.4, -0.3, -0.1, -0.2, 0.4, 0.2],
                [30500, 0.1, 0.0, 0.1, 0.06, 0.5, 0.7, 0.1, 0.2, 0.2, 0.7, 0.1],
                [29000, -1.5, 0.9, 0.7, 0.78, 0.6, 0.8, 0.4, 0.6, 0.4, 0.8, 0.5],
                [32500, 1.4, -0.8, -0.7, -0.74, 0.1, 0.3, -0.5, 0.0, -0.3, 0.2, 0.6],
                [31500, 0.0, 0.2, 0.2, 0.2, 0.4, 0.5, 0.0, 0.1, 0.1, 0.5, 0.2],
            ]
        )
        y = np.array([1, 2, 0, 1, 2, 0])  # buy, sell, hold
        pipeline = Pipeline(
            [
                ("imputer", SimpleImputer(strategy="median")),
                ("scaler", StandardScaler()),
                ("clf", RandomForestClassifier(n_estimators=80, random_state=42)),
            ]
        )
        pipeline.fit(X, y)
        model_id = "bootstrap-rf"
        model_path = models_path / f"{model_id}.joblib"
        scaler_path = models_path / f"{model_id}-pipeline.joblib"
        joblib.dump(pipeline, model_path)
        joblib.dump(pipeline, scaler_path)
        self.db.save_model_metadata(
            {
                "id": model_id,
                "name": "Bootstrap Random Forest",
                "model_type": "random_forest",
                "is_active": True,
                "created_at": iso_now(),
                "updated_at": iso_now(),
                "metrics": {"note": "modelo inicial para permitir inferência imediata"},
                "feature_order": FEATURE_ORDER,
                "model_path": str(model_path),
                "scaler_path": str(scaler_path),
            }
        )

    def list_models(self) -> list[dict[str, Any]]:
        return self.db.list_models()

    def load_active(self) -> tuple[dict[str, Any] | None, Any | None]:
        metadata = self.db.get_active_model()
        if not metadata:
            return None, None
        model_id = metadata["id"]
        if model_id not in self.loaded_cache:
            pipeline = joblib.load(metadata["model_path"])
            self.loaded_cache[model_id] = (pipeline, pipeline)
        pipeline, _ = self.loaded_cache[model_id]
        return metadata, pipeline

    def set_active(self, model_id: str) -> None:
        self.db.set_active_model(model_id)

    def delete(self, model_id: str) -> None:
        models = {row["id"]: row for row in self.db.list_models()}
        metadata = models.get(model_id)
        if not metadata:
            return
        for path_key in ("model_path", "scaler_path"):
            path = Path(metadata[path_key])
            if path.exists():
                path.unlink()
        self.db.execute("DELETE FROM models_metadata WHERE id = ?", (model_id,))
        self.loaded_cache.pop(model_id, None)

    def predict(self, feature_row: dict[str, float]) -> dict[str, Any]:
        metadata, pipeline = self.load_active()
        if metadata is None or pipeline is None:
            return {"action": "hold", "confidence": 0.0, "probabilities": {"buy": 0.0, "sell": 0.0, "hold": 1.0}}
        vector = np.array([[feature_row.get(feature, 0.0) for feature in FEATURE_ORDER]])
        probabilities = pipeline.predict_proba(vector)[0]
        classes = list(pipeline.named_steps["clf"].classes_)
        class_to_name = {0: "hold", 1: "buy", 2: "sell"}
        probs = {class_to_name[c]: float(probabilities[i]) for i, c in enumerate(classes)}
        for label in ("buy", "sell", "hold"):
            probs.setdefault(label, 0.0)
        action = max(probs, key=probs.get)
        return {
            "model_id": metadata["id"],
            "model_name": metadata["name"],
            "action": action,
            "confidence": float(probs[action]),
            "probabilities": probs,
            "feature_order": FEATURE_ORDER,
        }

    def train_model(self, model_name: str, model_type: str, training_rows: list[dict[str, Any]], random_state: int = 42, test_size: float = 0.2) -> dict[str, Any]:
        if len(training_rows) < 30:
            raise ValueError("Dados insuficientes para treino. Colete mais candles/features antes de treinar.")

        X = []
        y = []
        for row in training_rows:
            data = row["data"]
            if "label" not in data:
                continue
            X.append([data.get(feature, 0.0) for feature in FEATURE_ORDER])
            y.append(int(data["label"]))

        if len(X) < 30:
            raise ValueError("Poucas linhas rotuladas para treino.")

        X = np.array(X, dtype=float)
        y = np.array(y, dtype=int)

        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=test_size, random_state=random_state, stratify=y if len(set(y)) > 1 else None
        )

        clf = self._build_classifier(model_type, random_state=random_state)
        pipeline = Pipeline(
            [
                ("imputer", SimpleImputer(strategy="median")),
                ("scaler", StandardScaler()),
                ("clf", clf),
            ]
        )
        pipeline.fit(X_train, y_train)

        y_prob = pipeline.predict_proba(X_test)
        y_pred = pipeline.predict(X_test)

        metrics = {
            "accuracy": float(accuracy_score(y_test, y_pred)),
            "precision_macro": float(precision_score(y_test, y_pred, average="macro", zero_division=0)),
            "recall_macro": float(recall_score(y_test, y_pred, average="macro", zero_division=0)),
            "f1_macro": float(f1_score(y_test, y_pred, average="macro", zero_division=0)),
            "log_loss": float(log_loss(y_test, y_prob, labels=sorted(set(y)))),
            "confusion_matrix": confusion_matrix(y_test, y_pred).tolist(),
            "class_labels": ["hold", "buy", "sell"],
            "train_size": int(len(X_train)),
            "test_size": int(len(X_test)),
        }

        config = self.config_manager.get()
        model_id = slugify(model_name) or f"{model_type}-{int(math.floor(np.random.rand() * 100000))}"
        models_path = Path(config["ai_model"]["models_path"])
        models_path.mkdir(parents=True, exist_ok=True)
        model_path = models_path / f"{model_id}.joblib"
        scaler_path = models_path / f"{model_id}-pipeline.joblib"
        joblib.dump(pipeline, model_path)
        joblib.dump(pipeline, scaler_path)

        metadata = {
            "id": model_id,
            "name": model_name,
            "model_type": model_type,
            "is_active": False,
            "created_at": iso_now(),
            "updated_at": iso_now(),
            "metrics": metrics,
            "feature_order": FEATURE_ORDER,
            "model_path": str(model_path),
            "scaler_path": str(scaler_path),
        }
        self.db.save_model_metadata(metadata)
        self.loaded_cache.pop(model_id, None)
        return metadata

    def _build_classifier(self, model_type: str, random_state: int) -> Any:
        normalized = (model_type or "").strip().lower()
        if normalized == "xgboost":
            if XGBClassifier is None:
                raise RuntimeError("xgboost não disponível. Verifique a instalação do pacote.")
            return XGBClassifier(
                n_estimators=160,
                max_depth=6,
                learning_rate=0.05,
                subsample=0.9,
                colsample_bytree=0.9,
                random_state=random_state,
                objective="multi:softprob",
                eval_metric="mlogloss",
            )
        return RandomForestClassifier(
            n_estimators=240,
            max_depth=10,
            min_samples_leaf=2,
            random_state=random_state,
            class_weight="balanced_subsample",
        )
