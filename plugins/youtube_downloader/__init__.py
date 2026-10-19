import os
import re
import shutil

from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QFileDialog
from PySide6.QtCore import QThread, Signal
from qfluentwidgets import (
    LineEdit, ComboBox, PushButton, PrimaryPushButton, BodyLabel,
    CheckBox, ProgressBar, InfoBar, InfoBarPosition,
)

from core.plugin_base import BasePlugin
from core.settings import CACHE_DIR
from core.logger import log, log_exception

# Форматы: подпись -> форматная строка yt-dlp.
# Раздельные видео+аудио дорожки склеиваются встроенным в приложение FFmpeg в mp4.
QUALITY_FORMATS = {
    "Максимальное доступное (до 4K)": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
    "1080p (Full HD)": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]",
    "720p (HD)": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[height<=720]",
    "480p": "bestvideo[height<=480]+bestaudio[ext=m4a]/best[height<=480]/best",
}

_SAFE_NAME = re.compile(r"[^\w\-. ]+", re.UNICODE)


def _safe_filename(name: str) -> str:
    """Убирает символы, недопустимые в именах файлов на Windows/Linux."""
    name = _SAFE_NAME.sub("_", name).strip(" ._")
    return name[:150] if name else "video"


class DownloadWorker(QThread):
    """Скачивание через yt-dlp в фоновом потоке, чтобы не блокировать UI."""
    progress_changed = Signal(int)   # процент 0..100
    status_changed = Signal(str)     # человекочитаемый статус
    finished_ok = Signal(str)        # путь к скачанному файлу
    failed = Signal(str)

    def __init__(self, url: str, fmt: str, cookies_file: str = "",
                 use_browser_cookies: bool = False, out_dir: str = None):
        super().__init__()
        self.url = url.strip()
        self.fmt = fmt
        self.cookies_file = cookies_file
        self.use_browser_cookies = use_browser_cookies
        self.out_dir = out_dir or os.path.join(CACHE_DIR, "downloads")
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def _hook(self, d):
        import yt_dlp
        if self._cancelled:
            raise yt_dlp.utils.DownloadCancelled("Отменено пользователем")

        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes", 0)
            if total:
                self.progress_changed.emit(int(done * 100 / total))
            speed = d.get("speed") or 0
            eta = d.get("eta")
            self.status_changed.emit(
                f"Загрузка... {done / 1048576:.1f} МБ"
                + (f" из {total / 1048576:.1f} МБ" if total else "")
                + (f" · {speed / 1048576:.1f} МБ/с" if speed else "")
                + (f" · осталось {eta} с" if eta else "")
            )
        elif d["status"] == "finished":
            self.status_changed.emit("Склейка видео- и аудиодорожки (FFmpeg)...")
            self.progress_changed.emit(99)

    def run(self):
        final_path = None
        try:
            import yt_dlp  # ленивый импорт: без зависимости приложение не падает

            os.makedirs(self.out_dir, exist_ok=True)

            opts = {
                "format": self.fmt,
                "outtmpl": os.path.join(self.out_dir, "%(title)s.%(ext)s"),
                "progress_hooks": [self._hook],
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                # merge_output_format требует ffmpeg в PATH — он уже нужен приложению
                "merge_output_format": "mp4",
            }
            if self.cookies_file and os.path.isfile(self.cookies_file):
                opts["cookiefile"] = self.cookies_file
                log(f"YouTube: используются cookies из {self.cookies_file}")
            if self.use_browser_cookies:
                # Чтение cookies из браузера может требовать подтверждения на Windows
                opts["cookiesfrombrowser"] = ("chrome",)

            self.status_changed.emit("Получение информации о видео...")
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(self.url, download=True)
                title = _safe_filename(info.get("title", "video"))
                for ext in ("mp4", "mkv", "webm"):
                    candidate = os.path.join(self.out_dir, f"{title}.{ext}")
                    if os.path.exists(candidate):
                        final_path = candidate
                        break
                if not final_path:
                    requested = info.get("requested_downloads") or []
                    if requested:
                        final_path = requested[0].get("filepath")

            if final_path and os.path.exists(final_path):
                size_mb = os.path.getsize(final_path) / 1048576
                log(f"YouTube: готово — {os.path.basename(final_path)} ({size_mb:.1f} МБ)")
                self.progress_changed.emit(100)
                self.finished_ok.emit(final_path)
            else:
                self.failed.emit("Файл не найден после загрузки (неизвестная ошибка yt-dlp).")

        except Exception as e:
            log_exception("YouTube: ошибка скачивания")
            msg = str(e)[:500]
            if "Sign in to confirm you're not a bot" in msg or "not a bot" in msg.lower():
                msg += ("\nСовет: YouTube блокирует запросы без авторизации. Включите «cookies "
                        "из браузера» или укажите файл cookies.txt (расширение Get Cookies.txt).")
            elif "yt-dlp" in msg and "pip install" in msg.lower():
                pass
            if final_path and os.path.exists(final_path):
                # Скачали, но склейка упала — отдаём то, что есть
                self.finished_ok.emit(final_path)
            else:
                self.failed.emit(msg)


class YouTubeDownloadPlugin(BasePlugin):
    """Модуль загрузки исходников с YouTube в максимальном качестве."""
    name = "Загрузка с YouTube"
    description = ("Скачивает видео с YouTube напрямую с серверов Google в максимальном "
                   "доступном качестве (yt-dlp) вместо посредников низкого качества. "
                   "Файл сохраняется в cache/downloads и подставляется как источник нарезки.")
    version = "1.0.0"

    def __init__(self):
        super().__init__()
        self.is_active = False
        self.quality = next(iter(QUALITY_FORMATS))
        self.cookies_file = ""
        self._worker = None
        # Колбэк приложения: callable(path) -> None, вызывается после успешной загрузки
        self.on_video_ready = None

    # ------------------------------------------------------------------ UI
    def create_ui(self, parent: QWidget) -> QWidget:
        container = QWidget(parent)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(8)

        self.chk_enable = CheckBox("Включить модуль загрузки с YouTube", container)
        self.chk_enable.setChecked(self.is_active)
        self.chk_enable.stateChanged.connect(self._on_toggle)
        layout.addWidget(self.chk_enable)

        self.ed_url = LineEdit(container)
        self.ed_url.setPlaceholderText("Вставьте ссылку на видео YouTube...")
        self.ed_url.setEnabled(False)
        layout.addWidget(self.ed_url)

        q_box = QHBoxLayout()
        q_box.addWidget(BodyLabel("Качество:", container))
        self.combo_quality = ComboBox(container)
        self.combo_quality.addItems(list(QUALITY_FORMATS.keys()))
        self.combo_quality.currentIndexChanged.connect(
            lambda i: setattr(self, "quality", list(QUALITY_FORMATS)[i])
        )
        self.combo_quality.setEnabled(False)
        q_box.addWidget(self.combo_quality, 1)
        layout.addLayout(q_box)

        self.chk_browser_cookies = CheckBox("Cookies из браузера Chrome (обход антибот-проверки)", container)
        self.chk_browser_cookies.setEnabled(False)
        layout.addWidget(self.chk_browser_cookies)

        ck_box = QHBoxLayout()
        self.btn_cookies = PushButton("Файл cookies.txt", container)
        self.btn_cookies.setEnabled(False)
        self.btn_cookies.clicked.connect(self._pick_cookies)
        self.lbl_cookies = BodyLabel("не задан", container)
        self.lbl_cookies.setWordWrap(True)
        ck_box.addWidget(self.btn_cookies)
        ck_box.addWidget(self.lbl_cookies, 1)
        layout.addLayout(ck_box)

        btn_box = QHBoxLayout()
        self.btn_download = PrimaryPushButton("Скачать видео", container)
        self.btn_download.setEnabled(False)
        self.btn_download.clicked.connect(self._start_download)
        self.btn_cancel = PushButton("Отмена", container)
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._cancel_download)
        btn_box.addWidget(self.btn_download, 1)
        btn_box.addWidget(self.btn_cancel)
        layout.addLayout(btn_box)

        self.progress = ProgressBar(container)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.lbl_status = BodyLabel("", container)
        self.lbl_status.setWordWrap(True)
        layout.addWidget(self.lbl_status)

        return container

    # ------------------------------------------------------------------ handlers
    def _on_toggle(self, state):
        self.is_active = bool(state)
        self._set_inputs_enabled(self._worker is None)
        if self.is_active and not shutil.which("ffmpeg"):
            self.lbl_status.setText("Внимание: FFmpeg не найден в PATH — склейка раздельных потоков будет недоступна.")

    def _set_inputs_enabled(self, free: bool):
        on = self.is_active and free
        for w in (self.ed_url, self.combo_quality, self.chk_browser_cookies, self.btn_cookies):
            w.setEnabled(on)
        self.btn_download.setEnabled(on)
        self.btn_cancel.setEnabled(not free and self.is_active)

    def _pick_cookies(self):
        path, _ = QFileDialog.getOpenFileName(
            self.ed_url, "Выберите экспорт cookies.txt (формат Netscape)", "",
            "Cookies (*.txt);;Все файлы (*)"
        )
        if path:
            self.cookies_file = path
            self.lbl_cookies.setText(os.path.basename(path))

    def _start_download(self):
        url = self.ed_url.text().strip()
        if not re.match(r"^https?://", url):
            InfoBar.warning("Внимание", "Введите корректную ссылку (https://...)",
                            parent=self.ed_url, position=InfoBarPosition.TOP)
            return

        try:
            import yt_dlp  # noqa: F401 — проверяем зависимость до запуска потока
        except ImportError:
            InfoBar.error("Ошибка", "Не установлен пакет yt-dlp.\nВыполните: pip install -U yt-dlp",
                          parent=self.ed_url, position=InfoBarPosition.TOP, duration=-1)
            return

        self._worker = DownloadWorker(
            url=url,
            fmt=QUALITY_FORMATS[self.combo_quality.currentText()],
            cookies_file=self.cookies_file,
            use_browser_cookies=self.chk_browser_cookies.isChecked(),
        )
        self._worker.status_changed.connect(lambda t: self.lbl_status.setText(t))
        self._worker.progress_changed.connect(self.progress.setValue)
        self._worker.finished_ok.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)

        log(f"YouTube: старт загрузки {url} [{self.combo_quality.currentText()}]")
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self._set_inputs_enabled(False)
        self._worker.start()

    def _cancel_download(self):
        if self._worker:
            self._worker.cancel()
            self.lbl_status.setText("Отмена...")

    def _on_done(self, path):
        self._cleanup_worker()
        self.progress.setVisible(False)
        InfoBar.success("Готово", f"Видео скачано: {os.path.basename(path)}",
                        parent=self.ed_url, position=InfoBarPosition.TOP)
        self.lbl_status.setText(f"Сохранено: {path}")
        if callable(self.on_video_ready):
            self.on_video_ready(path)

    def _on_failed(self, err):
        self._cleanup_worker()
        self.progress.setVisible(False)
        self.lbl_status.setText(f"Ошибка: {err}")
        InfoBar.error("Скачивание не удалось", err,
                      parent=self.ed_url, position=InfoBarPosition.TOP, duration=-1)

    def _cleanup_worker(self):
        if self._worker:
            self._worker.wait(3000)
            self._worker = None
        self._set_inputs_enabled(True)

    # ------------------------------------------------------------------ hooks
    def modify_filter_graph(self, current_filter: str, segment_index: int, total_segments: int) -> str:
        # Модуль работает на этапе подготовки источника, фильтры кадра не меняет.
        return current_filter
