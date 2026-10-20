"""
Обучаемая модель «интересности» момента.

Это не нейросеть из интернета, а собственный линейный классификатор
(логистическая регрессия на NumPy) с обучением online-градиентным спуском:
  score = sigmoid(w · x + b),  x — вектор признаков окна (+ контекст ±N окон).

Почему так:
  * решение полностью прозрачное — веса w показывают, что именно ценится;
  * дообучение тривиально: каждый подтверждённый/отклонённый фрагмент
    превращается в пример (x, y) и делает несколько шагов SGD;
  * обучение занимает миллисекунды даже на тысячах примеров;
  * при желании поверх той же векторизации x можно построить более сложную
    модель (MLP, GBDT) — интерфейс ScoreModel уже это позволяет.

Хранение: JSON cache/moment_model.json — переносимый «опыт» приложения.
"""
import os
import json
import time
import threading

import numpy as np

FEATURES = ("motion", "brightness", "cut", "rms", "speech", "pitch_var", "tempo")
CONTEXT = 2          # сколько соседних окон слева/справа входит в вектор примера


def vectorize(feats: dict, i: int) -> np.ndarray:
    """Вектор признаков для окна i: сами признаки + среднее по контексту."""
    n = len(feats["times"])
    vec = []
    for k in FEATURES:
        series = np.asarray(feats[k], dtype=np.float64)
        lo, hi = max(0, i - CONTEXT), min(n, i + CONTEXT + 1)
        seg = series[lo:hi]
        vec.append(series[i])
        vec.append(float(seg.mean()))
        vec.append(float(seg.max()))
    return np.asarray(vec, dtype=np.float64)


class ScoreModel:
    """Линейная модель интересности с онлайн-дообучением."""

    def __init__(self, model_path: str):
        self.model_path = model_path
        # точная размерность вектора вычисляется лениво по первому примеру
        self.dim = None
        self.w = None
        self.b = 0.0
        self.lr = 0.15
        self.l2 = 1e-4
        self.n_samples = 0
        self.history = []           # [{t, acc_before}] — метрика прогресса обучения
        self._lock = threading.Lock()
        self.load()

    # ------------------------------------------------------------------ core
    def _ensure_dim(self, x: np.ndarray):
        if self.dim is None:
            self.dim = x.shape[0]
            self.w = np.zeros(self.dim)
            self.b = 0.0

    def predict(self, x: np.ndarray) -> float:
        """Вероятность того, что момент интересен (0..1)."""
        with self._lock:
            if self.w is None or self.dim != x.shape[0]:
                return 0.5
            z = float(np.dot(self.w, x) + self.b)
            z = max(-30.0, min(30.0, z))
            return 1.0 / (1.0 + np.exp(-z))

    def train_one(self, x: np.ndarray, y: float, epochs: int = 8):
        """Шаг дообучения на одном примере (y = 1 подтверждён, 0 отклонён)."""
        with self._lock:
            self._ensure_dim(x)
            acc = self._accuracy_on_stored(x, y)
            for _ in range(epochs):
                p = 1.0 / (1.0 + np.exp(-max(-30, min(30, np.dot(self.w, x) + self.b))))
                err = p - y
                self.w -= self.lr * (err * x + self.l2 * self.w)
                self.b -= self.lr * err
            self.n_samples += 1
            self.history.append({
                "t": time.time(), "y": float(y),
                "acc_before": acc,
                "features": {k: round(float(v), 3) for k, v in zip(FEATURES, x[:len(FEATURES)])},
            })
            self.save()

    def train_batch(self, X: np.ndarray, Y: np.ndarray, epochs: int = 30):
        """Полноценное обучение на матрице примеров (для пакетного дообучения)."""
        with self._lock:
            self._ensure_dim(X[0])
            rng = np.random.default_rng(42)
            m = len(X)
            for ep in range(epochs):
                order = rng.permutation(m)
                for idx in order:
                    x, y = X[idx], Y[idx]
                    p = 1.0 / (1.0 + np.exp(-max(-30, min(30, np.dot(self.w, x) + self.b))))
                    err = p - y
                    g = self.lr / m
                    self.w -= g * (err * x + self.l2 * self.w)
                    self.b -= g * err
            self.n_samples += m
            self.save()

    def _accuracy_on_stored(self, x, y):
        p = self.predict(x)
        return 1 if (p >= 0.5) == (y >= 0.5) else 0

    # ------------------------------------------------------------------ persist
    def save(self):
        try:
            data = {
                "dim": self.dim,
                "w": self.w.tolist() if self.w is not None else None,
                "b": self.b,
                "lr": self.lr, "l2": self.l2,
                "n_samples": self.n_samples,
                "history": self.history[-2000:],
                "features": FEATURES, "context": CONTEXT,
                "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            os.makedirs(os.path.dirname(self.model_path), exist_ok=True)
            tmp = self.model_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self.model_path)
        except OSError:
            pass

    def load(self):
        if not os.path.exists(self.model_path):
            return
        try:
            with open(self.model_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.dim = data.get("dim")
            w = data.get("w")
            self.w = np.asarray(w, dtype=np.float64) if w else None
            self.b = float(data.get("b", 0.0))
            self.lr = float(data.get("lr", 0.15))
            self.l2 = float(data.get("l2", 1e-4))
            self.n_samples = int(data.get("n_samples", 0))
            self.history = data.get("history", [])
        except (ValueError, OSError, KeyError):
            pass

    def reset(self):
        with self._lock:
            self.dim = None
            self.w = None
            self.b = 0.0
            self.n_samples = 0
            self.history = []
            self.save()

    # ------------------------------------------------------------------ info
    def weights_report(self) -> list:
        """Человекочитаемый вклад базовых признаков (сумма весов по группе)."""
        if self.w is None:
            return []
        rows = []
        for gi, name in enumerate(FEATURES):
            sl = slice(gi * 3, gi * 3 + 3)
            rows.append((name, float(self.w[sl].sum())))
        rows.sort(key=lambda r: -abs(r[1]))
        return rows
