"""
Кэш признаков и база обратной связи (примеры обучения).

  cache/moments/features/<hash>.json   — съёмка признаков по видео (не пересчитывать)
  cache/moments/feedback.json          — накопленный «опыт»: вектор + метка + мета
"""
import os
import json
import hashlib
import threading

_MAX_FEEDBACK = 5000


def video_hash(path: str) -> str:
    st = os.stat(path)
    key = f"{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


class Store:
    def __init__(self, base_dir: str):
        self.feat_dir = os.path.join(base_dir, "features")
        self.fb_path = os.path.join(base_dir, "feedback.json")
        os.makedirs(self.feat_dir, exist_ok=True)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ feats
    def feat_file(self, path: str, window: float) -> str:
        return os.path.join(self.feat_dir, f"{video_hash(path)}_w{window:g}.json")

    def load_features(self, path: str, window: float):
        fp = self.feat_file(path, window)
        if not os.path.exists(fp):
            return None
        try:
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("source") == os.path.abspath(path):
                return data["feats"]
        except (ValueError, OSError, KeyError):
            pass
        return None

    def save_features(self, path: str, window: float, feats: dict):
        fp = self.feat_file(path, window)
        try:
            with open(fp, "w", encoding="utf-8") as f:
                json.dump({"source": os.path.abspath(path),
                           "created": __import__("time").strftime("%Y-%m-%d %H:%M"),
                           "feats": feats}, f)
        except OSError:
            pass

    # ------------------------------------------------------------------ feedback
    def load_feedback(self) -> list:
        if not os.path.exists(self.fb_path):
            return []
        try:
            with open(self.fb_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (ValueError, OSError):
            return []

    def append_feedback(self, record: dict):
        """record: {x:[...], y:0/1, video, start, end, score, ts}"""
        with self._lock:
            data = self.load_feedback()
            data.append(record)
            if len(data) > _MAX_FEEDBACK:
                data = data[-_MAX_FEEDBACK:]
            tmp = self.fb_path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                os.replace(tmp, self.fb_path)
            except OSError:
                pass

    def clear_feedback(self):
        with self._lock:
            try:
                if os.path.exists(self.fb_path):
                    os.remove(self.fb_path)
            except OSError:
                pass
