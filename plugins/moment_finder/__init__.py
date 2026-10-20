"""
Плагин «Искатель моментов» (Moment Finder).

Собственный обучаемый алгоритм без готовых нейросетей:
  1. FFmpeg+NumPy снимают сигнальные признаки видео (движение, склейки,
     громкость, речь, интонации) — см. features.py;
  2. собственный линейный классификатор (логистическая регрессия, NumPy)
     оценивает «интересность» каждого окна — см. model.py;
  3. scorer.py превращает кривую оценок в конкретные моменты (пики + NMS);
  4. пользователь подтверждает/отклоняет моменты в AE-подобном интерфейсе
     (studio.py) → каждый клик дообучает модель (online SGD), опыт хранится
     в cache/moments/ и переносится на новые видео.

TODO(зарезервировано): внутренняя система расширений редактора моментов
(панель эффектов, экспрешны) — MomentStudio.register_tool().
"""
import os
import sys
import json
import subprocess
import threading

from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout
from PySide6.QtCore import QThread, Signal, QObject
from qfluentwidgets import (
    CardWidget, BodyLabel, StrongBodyLabel, PrimaryPushButton, PushButton,
    CheckBox, InfoBar, InfoBarPosition, FluentIcon, ProgressBar
)

# PluginManager добавляет папку plugins в sys.path — импортируем как пакет
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from .features import FeatureExtractor
    from .model import ScoreModel, vectorize, FEATURES
    from .scorer import find_moments, heuristic_score, gaussian_smooth
    from .store import Store
except ImportError:  # загрузка как standalone-модуля
    from features import FeatureExtractor
    from model import ScoreModel, vectorize, FEATURES
    from scorer import find_moments, heuristic_score, gaussian_smooth
    from store import Store

from core.plugin_base import BasePlugin
from core.settings import CACHE_DIR
from core.logger import log


class _CutWorker(QObject):
    """Нарезка подтверждённых моментов отдельным ffmpeg-процессом."""
    progress = Signal(int, int)        # cur, total
    done = Signal(str)                 # итоговая строка
    failed = Signal(str)

    def __init__(self, video, moments, out_dir):
        super().__init__()
        self.video, self.moments, self.out_dir = video, moments, out_dir
        self.cancelled = False

    def run(self):
        try:
            base = os.path.splitext(os.path.basename(self.video))[0]
            ok = 0
            for i, m in enumerate(self.moments):
                if self.cancelled:
                    break
                out = os.path.join(self.out_dir, f"{base}_moment_{i+1:02d}.mp4")
                cmd = ["ffmpeg", "-y", "-v", "error",
                       "-ss", f"{m['start']:.2f}", "-to", f"{m['end']:.2f}",
                       "-i", self.video,
                       "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                       "-c:a", "aac", "-movflags", "+faststart", out]
                r = subprocess.run(cmd, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace",
                                   creationflags=subprocess.CREATE_NO_WINDOW
                                   if sys.platform == "win32" else 0)
                if r.returncode == 0:
                    ok += 1
                self.progress.emit(i + 1, len(self.moments))
            self.done.emit(f"Нарезано моментов: {ok}/{len(self.moments)} → {self.out_dir}")
        except Exception as e:
            self.failed.emit(str(e))


class AnalysisWorker(QThread):
    """Фоновая съёмка признаков (тяжёлая часть — декод + FFT)."""
    progress = Signal(int, str)
    finished_ok = Signal(dict)
    failed = Signal(str)

    def __init__(self, video_path, window):
        super().__init__()
        self.video_path = video_path
        self.window = window

    def run(self):
        try:
            fx = FeatureExtractor()
            feats = fx.extract(self.video_path, window=self.window,
                               sample_fps=2.0,
                               progress=lambda p: self.progress.emit(
                                   int(p * 100), "Съёмка признаков"))
            self.finished_ok.emit(feats)
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")


class MomentFinderPlugin(BasePlugin):
    name = "Искатель моментов (обучаемый)"
    description = ("Автоматически находит самые интересные фрагменты длинного видео. "
                   "Подтверждайте или отклоняйте моменты — алгоритм запоминает ваши "
                   "критерии и со временем подбирает лучше.")
    version = "1.0.0"

    PARAMS_PATH = os.path.join(CACHE_DIR, "moments", "params.json")

    def __init__(self):
        super().__init__()
        self.is_active = True
        self.base_dir = os.path.join(CACHE_DIR, "moments")
        os.makedirs(self.base_dir, exist_ok=True)
        self.store = Store(self.base_dir)
        self.model = ScoreModel(os.path.join(self.base_dir, "moment_model.json"))
        self.params = {"window": 1.0, "min_len": 15.0, "max_len": 60.0,
                       "threshold": 0.45, "top_k": 12}
        self._load_params()
        self._worker = None
        self._studio = None
        self._cut_thread = None
        self._card = None
        self.on_cut_done = None          # колбэк от главного окна (опционально)
        # источник видео: задаётся главным окном через on_video_ready
        # (например, скачанный с YouTube ролик) или кнопкой «Выбрать видео»
        self.on_video_ready = None
        self._last_video = ""

    # ------------------------------------------------------------------ params
    def set_source(self, path: str):
        """Главное окно сообщает актуальный источник видео (локальный или из YouTube)."""
        if path and os.path.exists(path):
            self._last_video = path
            if self._card is not None and hasattr(self, "lbl_video"):
                self.lbl_video.setText(f"Источник: {os.path.basename(path)}")

    def _load_params(self):
        try:
            if os.path.exists(self.PARAMS_PATH):
                with open(self.PARAMS_PATH, "r", encoding="utf-8") as f:
                    self.params.update(json.load(f))
        except (ValueError, OSError):
            pass

    def save_params(self):
        try:
            os.makedirs(os.path.dirname(self.PARAMS_PATH), exist_ok=True)
            with open(self.PARAMS_PATH, "w", encoding="utf-8") as f:
                json.dump(self.params, f)
        except OSError:
            pass

    # ------------------------------------------------------------------ analysis API
    def analyze(self, video_path, on_progress, on_done):
        """Запуск фоновой съёмки признаков для видео."""
        cached = self.store.load_features(video_path, self.params["window"])
        if cached:
            on_progress(100, "Из кэша")
            on_done(cached)
            return
        self._worker = AnalysisWorker(video_path, self.params["window"])
        self._worker.progress.connect(on_progress)
        self._worker.finished_ok.connect(lambda f: self._on_analyzed(video_path, f, on_done))
        self._worker.failed.connect(lambda e: log(f"MomentFinder: ошибка анализа: {e}"))
        self._worker.start()

    def _on_analyzed(self, video_path, feats, on_done):
        self.store.save_features(video_path, self.params["window"], feats)
        log(f"MomentFinder: признаки сняты ({feats['duration']:.0f} c, "
            f"{len(feats['times'])} окон)")
        on_done(feats)

    def is_analyzing(self):
        return bool(self._worker and self._worker.isRunning())

    def features_ready(self, video_path):
        return self.store.load_features(video_path, self.params["window"]) is not None

    def get_cached_features(self, video_path):
        return self.store.load_features(video_path, self.params["window"])

    def find_moments(self, video_path, feats, params=None):
        p = dict(self.params)
        p.update(params or {})
        return find_moments(feats, self.model, p)

    # ------------------------------------------------------------------ learning API
    def learn_from(self, moment, x, accepted: bool, video_path: str):
        """Один шаг дообучения + запись примера в базу опыта."""
        record = {"x": [float(v) for v in x], "y": 1 if accepted else 0,
                  "video": os.path.basename(video_path),
                  "start": moment["start"], "end": moment["end"],
                  "score": moment.get("peak")}
        self.store.append_feedback(record)
        self.model.train_one(x, 1.0 if accepted else 0.0)
        log(f"MomentFinder: обучение {'+' if accepted else '-'} "
            f"[{moment['start']:.0f}-{moment['end']:.0f} c], "
            f"примеров: {self.model.n_samples}")

    def retrain_from_feedback(self) -> int:
        """Пакетное переобучение на всём накопленном опыте."""
        data = self.store.load_feedback()
        if not data:
            return 0
        import numpy as np
        X = np.asarray([d["x"] for d in data], dtype=np.float64)
        Y = np.asarray([float(d["y"]) for d in data], dtype=np.float64)
        self.model.train_batch(X, Y, epochs=40)
        log(f"MomentFinder: переобучение на {len(data)} примерах завершена")
        return len(data)

    # ------------------------------------------------------------------ studio
    def open_studio(self, video_path: str):
        if self._studio is not None:
            try:
                self._studio.raise_()
                self._studio.activateWindow()
                return self._studio
            except RuntimeError:
                self._studio = None
        try:
            try:
                from .studio import MomentStudio
            except ImportError:
                from studio import MomentStudio
        except ImportError as e:
            raise RuntimeError(f"Модуль интерфейса не загрузился: {e}")
        self._studio = MomentStudio(self, video_path)
        self._studio.request_cut.connect(self.cut_moments)
        self._studio.show()
        return self._studio

    # ------------------------------------------------------------------ cutting
    def cut_moments(self, moments):
        video = getattr(self, "_last_video", "")
        if not video or not os.path.exists(video):
            InfoBar.warning("Нет видео", "Сначала проанализируйте видео.",
                            parent=self._card, position=InfoBarPosition.TOP)
            return
        out_dir = os.path.join(self.base_dir, "cuts",
                               os.path.splitext(os.path.basename(video))[0])
        os.makedirs(out_dir, exist_ok=True)
        worker_obj = _CutWorker(video, moments, out_dir)
        self._cut_thread = QThread()
        worker_obj.moveToThread(self._cut_thread)
        self._cut_thread.started.connect(worker_obj.run)
        worker_obj.progress.connect(self._on_cut_progress)
        worker_obj.done.connect(lambda t: self._on_cut_done(t, worker_obj))
        worker_obj.failed.connect(lambda e: log(f"MomentFinder: ошибка резки: {e}"))
        self._cut_thread.start()
        log(f"MomentFinder: нарезка {len(moments)} подтверждённых моментов")

    def _on_cut_progress(self, cur, tot):
        if self._card is not None and hasattr(self, "prog_cut"):
            self.prog_cut.setMaximum(max(tot, 1))
            self.prog_cut.setValue(cur)

    def _on_cut_done(self, text, worker_obj):
        log(f"MomentFinder: {text}")
        if self._card is not None:
            InfoBar.success("Готово", text, parent=self._card,
                            position=InfoBarPosition.TOP, duration=8000)
        if self._cut_thread is not None:
            self._cut_thread.quit()
            self._cut_thread.wait(3000)
        if callable(self.on_cut_done):
            try:
                self.on_cut_done(text)
            except Exception:
                pass

    # ------------------------------------------------------------------ UI card
    def create_ui(self, parent: QWidget) -> QWidget:
        container = QWidget(parent)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(8)

        head = QHBoxLayout()
        chk = CheckBox("Включить модуль", container)
        chk.setChecked(True)
        chk.stateChanged.connect(lambda s: setattr(self, "is_active", bool(s)))
        head.addWidget(chk)
        head.addStretch(1)
        layout.addLayout(head)

        info_row = QHBoxLayout()
        self.btn_pick = PushButton(FluentIcon.FOLDER_ADD, "Выбрать видео", container)
        self.btn_pick.clicked.connect(self._pick_video)
        info_row.addWidget(self.btn_pick)
        self.btn_open = PrimaryPushButton(FluentIcon.PLAY_SOLID, "Открыть интерфейс", container)
        self.btn_open.clicked.connect(self._open_ui)
        info_row.addWidget(self.btn_open)
        layout.addLayout(info_row)

        self.lbl_video = BodyLabel("Видео не выбрано", container)
        self.lbl_video.setWordWrap(True)
        layout.addWidget(self.lbl_video)

        self.lbl_state = StrongBodyLabel("", container)
        self.lbl_state.setWordWrap(True)
        layout.addWidget(self.lbl_state)

        self.prog_cut = ProgressBar(container)
        self.prog_cut.setVisible(False)
        layout.addWidget(self.prog_cut)

        n_fb = len(self.store.load_feedback())
        if self.model.n_samples or n_fb:
            rep = self.model.weights_report()
            top = ", ".join(f"{k} {v:+.2f}" for k, v in rep[:3]) if rep else "—"
            self.lbl_state.setText(
                f"Опыт: {self.model.n_samples} размок · примеры: {n_fb}\n"
                f"Что ценится: {top}")
        else:
            self.lbl_state.setText(
                "Модель ещё ничему не научилась. Откройте интерфейс, подтвердите "
                "интересные моменты — и она запомнит ваши критерии.")
        self._card = container
        return container

    def _pick_video(self):
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self._card, "Выберите видео для поиска моментов", "",
            "Video (*.mp4 *.mkv *.mov *.avi *.webm)")
        if path:
            self._last_video = path
            self.lbl_video.setText(f"Источник: {os.path.basename(path)}")

    def _open_ui(self):
        video = getattr(self, "_last_video", "")
        if not video or not os.path.exists(video):
            InfoBar.warning("Нет видео", "Сначала нажмите «Выбрать видео».",
                            parent=self._card, position=InfoBarPosition.TOP)
            return
        try:
            self.open_studio(video)
        except RuntimeError as e:
            InfoBar.error("Ошибка", str(e), parent=self._card,
                          position=InfoBarPosition.TOP, duration=-1)

    # конвейер рендера плагином не модифицируется — моменты режутся отдельно
    def modify_filter_graph(self, current_filter: str, segment_index: int,
                            total_segments: int) -> str:
        return current_filter
