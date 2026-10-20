"""
Поиск и нарезка «моментов» по кривой интересности.

Алгоритм:
 1. score[i] = predict(vectorize(feats, i)) для каждого окна;
    до обучения используется эвристика-бустер (комбинация признаков),
    чтобы первый запуск уже выдавал разумные кандидаты;
 2. гауссово сглаживание кривой (момент не может быть точечным);
 3. поиск локальных максимумов выше порога;
 4. расширение пика до границ min_len..max_len по «площадке» вокруг вершины;
 5. Non-Maximum Suppression: перекрывающиеся кандидаты сливаются,
    остаётся сильнейший; всего не больше top_k.
"""
import numpy as np

from .model import ScoreModel, vectorize, FEATURES


def heuristic_score(feats: dict) -> np.ndarray:
    """Базовая «интуиция» до обучения: динамика + голос + всплески."""
    n = len(feats["times"])
    m = np.asarray(feats["motion"]); c = np.asarray(feats["cut"])
    r = np.asarray(feats["rms"]); s = np.asarray(feats["speech"])
    p = np.asarray(feats["pitch_var"]); t = np.asarray(feats["tempo"])
    return np.clip(0.28 * m + 0.22 * s + 0.16 * r + 0.14 * c + 0.10 * p + 0.10 * t, 0, 1)


def gaussian_smooth(x: np.ndarray, sigma: float = 2.0) -> np.ndarray:
    radius = max(1, int(sigma * 3))
    k = np.exp(-0.5 * (np.arange(-radius, radius + 1) / sigma) ** 2)
    k /= k.sum()
    pad = np.pad(x, radius, mode="edge")
    return np.convolve(pad, k, mode="valid")


def find_moments(feats: dict, model: ScoreModel, params: dict) -> list:
    """Возвращает список [{'start','end','peak','score'}] в секундах."""
    window = float(feats["window"])
    times = np.asarray(feats["times"])
    duration = float(feats["duration"])
    min_len = float(params.get("min_len", 15.0))
    max_len = float(params.get("max_len", 60.0))
    threshold = float(params.get("threshold", 0.45))
    top_k = int(params.get("top_k", 12))

    trained = model.w is not None and model.n_samples > 0
    if trained:
        scores = np.asarray([model.predict(vectorize(feats, i)) for i in range(len(times))])
    else:
        scores = heuristic_score(feats)

    # сглаживание: окно ~ половина минимальной длины момента
    sigma = max(1.0, min_len / window / 4.0)
    curve = gaussian_smooth(scores, sigma)

    half = max(1, int(min_len / window / 2))
    cand = []
    for i in range(len(curve)):
        lo, hi = max(0, i - half), min(len(curve), i + half + 1)
        if curve[i] >= threshold and curve[i] == curve[lo:hi].max():
            cand.append((i, float(curve[i]), float(scores[i])))

    # расширение до площадки: идём влево/вправо пока кривая не упадёт ниже 60% пика
    def expand(i, peak):
        floor = peak * 0.6
        l = i
        while l > 0 and curve[l] >= floor:
            l -= 1
        r = i
        while r < len(curve) - 1 and curve[r] >= floor:
            r += 1
        return l, r

    raw = []
    used = np.zeros(len(curve), bool)
    for i, cv, sc in sorted(cand, key=lambda x: -x[1]):
        l, r = expand(i, cv)
        length = (r - l + 1) * window
        # подгонка длины в норматив min..max
        if length < min_len:
            extra = int(((min_len - length) / window) / 2)
            l, r = max(0, l - extra), min(len(curve) - 1, r + extra)
        if (r - l + 1) * window > max_len:
            center = (l + r) / 2
            keep = int(max_len / window / 2)
            l, r = max(0, int(center - keep)), min(len(curve) - 1, int(center + keep))
        if used[l:r + 1].any():
            continue
        used[l:r + 1] = True
        start = max(0.0, l * window)
        end = min(duration, (r + 1) * window)
        if end - start >= min_len * 0.7:
            raw.append({"start": round(start, 2), "end": round(end, 2),
                        "peak": round(cv, 4), "score": round(sc, 4)})

    raw.sort(key=lambda m: m["start"])
    return raw[:top_k]
