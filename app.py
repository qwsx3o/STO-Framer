import os
import sys
import shutil
import traceback
from PySide6.QtWidgets import (
    QApplication, QWidget, QHBoxLayout, QVBoxLayout, QFileDialog, QPlainTextEdit
)
from PySide6.QtCore import Qt, QTimer, Signal, QRect, QRectF
from PySide6.QtGui import (
    QPixmap, QColor, QPalette, QPainter, QPainterPath, QPen, QFont,
    QFontDatabase, QLinearGradient
)
from qfluentwidgets import (
    setTheme, Theme, CardWidget, SubtitleLabel,
    BodyLabel, PushButton, PrimaryPushButton, Slider, SwitchButton,
    LineEdit, ComboBox, ProgressBar, InfoBar, InfoBarPosition, ScrollArea
)

from core.engine import VideoEngine
from core.worker import RenderWorker
from core.plugin_manager import PluginManager
from core.settings import Settings, FONTS_DIR, CACHE_DIR
from core.logger import bus, log, reset_log

WHISPER_MODELS = ["tiny", "base", "small", "medium"]
LANG_LABELS = ["Авто", "Русский", "English"]
LANG_CODES = ["auto", "ru", "en"]


class PreviewCanvas(QWidget):
    """Превью 9:16: кадр видео + перетаскиваемый пример субтитров."""
    pos_changed = Signal(float, float)
    drag_finished = Signal()

    SAMPLE = "Пример субтитров"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(270, 480)
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self._frame = None
        self._message = "Загрузите видео\nдля предпросмотра"
        self._family = ""
        self._size = 72
        self._nx = 0.5
        self._ny = 0.75
        self._subs_visible = True
        self._dragging = False

    def set_frame(self, pixmap):
        self._frame = pixmap
        self.update()

    def set_message(self, text):
        self._message = text
        self.update()

    def set_font_family(self, family):
        self._family = family
        self.update()

    def set_font_size(self, size):
        self._size = size
        self.update()

    def set_pos(self, nx, ny):
        self._nx, self._ny = nx, ny
        self.update()

    def set_subs_visible(self, visible):
        self._subs_visible = visible
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        w, h = self.width(), self.height()
        rect = self.rect()

        clip = QPainterPath()
        clip.addRoundedRect(QRectF(rect), 12, 12)
        p.setClipPath(clip)

        if self._frame is not None and not self._frame.isNull():
            p.drawPixmap(rect, self._frame)
        else:
            grad = QLinearGradient(0, 0, 0, h)
            grad.setColorAt(0.0, QColor(46, 46, 58))
            grad.setColorAt(1.0, QColor(18, 18, 24))
            p.fillRect(rect, grad)
            p.setPen(QColor(150, 150, 165))
            p.drawText(QRect(0, 40, w, 80), Qt.AlignmentFlag.AlignCenter, self._message)

        if self._subs_visible:
            scale = h / 1920.0
            font = QFont(self._family) if self._family else QFont()
            font.setPixelSize(max(6, int(self._size * scale)))
            path = QPainterPath()
            path.addText(0, 0, font, self.SAMPLE)
            br = path.boundingRect()
            path.translate(self._nx * w - br.center().x(), self._ny * h - br.center().y())

            pen = QPen(QColor(0, 0, 0), max(1.0, 2 * 5 * scale))
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.strokePath(path, pen)
            p.fillPath(path, QColor(255, 255, 255))

        p.setClipping(False)
        p.setPen(QPen(QColor(60, 60, 60), 1))
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 12, 12)

    def _move_to(self, pos):
        nx = min(0.95, max(0.05, pos.x() / self.width()))
        ny = min(0.97, max(0.03, pos.y() / self.height()))
        self._nx, self._ny = nx, ny
        self.update()
        self.pos_changed.emit(nx, ny)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._move_to(event.position())

    def mouseMoveEvent(self, event):
        if self._dragging:
            self._move_to(event.position())

    def mouseReleaseEvent(self, event):
        if self._dragging:
            self._dragging = False
            self.drag_finished.emit()


class StudioWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("StudioMainWindow")
        self.setWindowTitle("STOFramer Studio 9:16")
        self.resize(1180, 980)

        self._apply_dark_theme()

        os.makedirs(FONTS_DIR, exist_ok=True)
        os.makedirs(CACHE_DIR, exist_ok=True)

        self.settings = Settings()

        # Файлы и пути
        self.input_video = ""
        self.output_directory = self.settings.get("output_dir") or ""
        if self.output_directory and not os.path.isdir(self.output_directory):
            self.output_directory = ""
        self.preview_cache_file = os.path.join(CACHE_DIR, "cache_preview.jpg")

        # Шрифты
        self.font_files = {}
        self._load_fonts()

        # Модули
        self.plugin_manager = PluginManager()
        self.plugins = self.plugin_manager.reload_plugins()

        # Дебаунс превью
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(300)
        self._preview_timer.timeout.connect(self._update_preview)

        self._init_layout()
        log(f"Приложение запущено. Шрифтов: {len(self.font_files)}, плагинов: {len(self.plugins)}")

    # ------------------------------------------------------------------ темы / шрифты
    def _apply_dark_theme(self):
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor(32, 32, 32))
        palette.setColor(QPalette.ColorRole.WindowText, QColor(255, 255, 255))
        self.setPalette(palette)
        self.setAutoFillBackground(True)

        self.setStyleSheet("""
            QWidget#StudioMainWindow {
                background-color: #202020;
            }
            CardWidget {
                background-color: #2b2b2b;
                border: 1px solid #383838;
                border-radius: 10px;
            }
        """)

    def _load_fonts(self):
        self.font_files.clear()
        for name in sorted(os.listdir(FONTS_DIR)):
            if name.lower().endswith((".ttf", ".otf", ".ttc")):
                path = os.path.join(FONTS_DIR, name)
                font_id = QFontDatabase.addApplicationFont(path)
                if font_id >= 0:
                    for fam in QFontDatabase.applicationFontFamilies(font_id):
                        self.font_files[fam] = path

    def _fill_font_combo(self, select=""):
        self.combo_font.blockSignals(True)
        self.combo_font.clear()
        families = sorted(self.font_files)
        self.combo_font.addItems(families)
        if families:
            self.combo_font.setCurrentIndex(families.index(select) if select in families else 0)
        self.combo_font.blockSignals(False)

    def _current_family(self):
        return self.combo_font.currentText() if self.font_files else ""

    # ------------------------------------------------------------------ UI
    def _init_layout(self):
        s = self.settings
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(20)

        # ----------------- ЛЕВАЯ КОЛОНКА (Настройки) -----------------
        scroll = ScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("""
            QScrollArea {
                border: none;
                background: transparent;
            }
            QScrollArea > QWidget > QWidget {
                background: transparent;
            }
        """)

        left_widget = QWidget()
        left_widget.setStyleSheet("background: transparent;")
        left_layout = QVBoxLayout(left_widget)
        left_layout.setSpacing(14)

        # 1. Источники файлов
        card_io = CardWidget(left_widget)
        io_layout = QVBoxLayout(card_io)
        io_layout.addWidget(SubtitleLabel("Источники и экспорт"))

        self.btn_select_file = PushButton("Выбрать исходный фильм / сериал")
        self.btn_select_file.clicked.connect(self._select_file)
        self.lbl_file = BodyLabel("Файл не выбран")
        io_layout.addWidget(self.btn_select_file)
        io_layout.addWidget(self.lbl_file)

        self.btn_select_dir = PushButton("Выбрать папку для нарезки")
        self.btn_select_dir.clicked.connect(self._select_dir)
        self.lbl_dir = BodyLabel(self.output_directory or "Папка не выбрана")
        io_layout.addWidget(self.btn_select_dir)
        io_layout.addWidget(self.lbl_dir)
        left_layout.addWidget(card_io)

        # 2. Параметры нарезки
        card_params = CardWidget(left_widget)
        params_layout = QVBoxLayout(card_params)
        params_layout.addWidget(SubtitleLabel("Параметры нарезки"))

        seg_val = int(s.get("seg_minutes"))
        self.lbl_seg_info = BodyLabel(f"Длительность одной части: {seg_val} минут")
        self.slider_seg = Slider(Qt.Orientation.Horizontal)
        self.slider_seg.setRange(1, 20)
        self.slider_seg.setValue(seg_val)
        self.slider_seg.valueChanged.connect(self._on_seg_changed)
        params_layout.addWidget(self.lbl_seg_info)
        params_layout.addWidget(self.slider_seg)

        blur_val = int(s.get("blur"))
        self.lbl_blur_info = BodyLabel(f"Сила размытия фона: {blur_val} px")
        self.slider_blur = Slider(Qt.Orientation.Horizontal)
        self.slider_blur.setRange(5, 70)
        self.slider_blur.setValue(blur_val)
        self.slider_blur.valueChanged.connect(self._on_blur_changed)
        params_layout.addWidget(self.lbl_blur_info)
        params_layout.addWidget(self.slider_blur)

        flip_box = QHBoxLayout()
        self.switch_hflip = SwitchButton()
        self.switch_hflip.setChecked(bool(s.get("hflip")))
        self.switch_hflip.checkedChanged.connect(self._on_hflip_changed)
        flip_box.addWidget(self.switch_hflip)
        flip_box.addWidget(BodyLabel("Отразить по горизонтали (зеркальный обход банов)"))
        params_layout.addLayout(flip_box)

        params_layout.addWidget(BodyLabel("Шаблон текста (например: 'Часть {part}'):"))
        self.txt_template = LineEdit()
        self.txt_template.setText(s.get("text_template"))
        self.txt_template.textChanged.connect(self._on_template_changed)
        params_layout.addWidget(self.txt_template)

        params_layout.addWidget(BodyLabel("Расположение текста:"))
        self.combo_pos = ComboBox()
        self.combo_pos.addItems(["Сверху кадра", "По центру", "Снизу кадра"])
        self.combo_pos.setCurrentIndex(min(2, max(0, int(s.get("text_pos_index")))))
        self.combo_pos.currentIndexChanged.connect(self._on_title_pos_changed)
        params_layout.addWidget(self.combo_pos)

        left_layout.addWidget(card_params)

        # 3. Автосубтитры
        card_subs = CardWidget(left_widget)
        subs_layout = QVBoxLayout(card_subs)
        subs_layout.addWidget(SubtitleLabel("Автосубтитры"))

        sw_box = QHBoxLayout()
        self.switch_subs = SwitchButton()
        self.switch_subs.setChecked(bool(s.get("subs_enabled")))
        self.switch_subs.checkedChanged.connect(self._on_subs_toggle)
        sw_box.addWidget(self.switch_subs)
        sw_box.addWidget(BodyLabel("Генерировать субтитры автоматически (Whisper)"))
        subs_layout.addLayout(sw_box)

        subs_layout.addWidget(BodyLabel("Шрифт субтитров:"))
        self.combo_font = ComboBox()
        self._fill_font_combo(s.get("subs_font_family"))
        self.combo_font.currentIndexChanged.connect(self._on_font_changed)
        subs_layout.addWidget(self.combo_font)

        self.btn_add_font = PushButton("Загрузить шрифт (.ttf / .otf)")
        self.btn_add_font.clicked.connect(self._add_fonts)
        subs_layout.addWidget(self.btn_add_font)

        size_val = int(s.get("subs_font_size"))
        self.lbl_font_size = BodyLabel(f"Размер субтитров: {size_val}")
        self.slider_font_size = Slider(Qt.Orientation.Horizontal)
        self.slider_font_size.setRange(30, 160)
        self.slider_font_size.setValue(size_val)
        self.slider_font_size.valueChanged.connect(self._on_font_size_changed)
        subs_layout.addWidget(self.lbl_font_size)
        subs_layout.addWidget(self.slider_font_size)

        subs_layout.addWidget(BodyLabel("Модель распознавания (больше — точнее, но медленнее):"))
        self.combo_model = ComboBox()
        self.combo_model.addItems(WHISPER_MODELS)
        model = s.get("whisper_model")
        self.combo_model.setCurrentIndex(WHISPER_MODELS.index(model) if model in WHISPER_MODELS else 2)
        self.combo_model.currentIndexChanged.connect(
            lambda i: self.settings.set("whisper_model", WHISPER_MODELS[i])
        )
        subs_layout.addWidget(self.combo_model)

        subs_layout.addWidget(BodyLabel("Язык речи:"))
        self.combo_lang = ComboBox()
        self.combo_lang.addItems(LANG_LABELS)
        lang = s.get("subs_language")
        self.combo_lang.setCurrentIndex(LANG_CODES.index(lang) if lang in LANG_CODES else 0)
        self.combo_lang.currentIndexChanged.connect(
            lambda i: self.settings.set("subs_language", LANG_CODES[i])
        )
        subs_layout.addWidget(self.combo_lang)

        subs_layout.addWidget(BodyLabel("Перетащите субтитры мышкой по окну предпросмотра."))
        left_layout.addWidget(card_subs)

        # 4. Модули
        card_plugins = CardWidget(left_widget)
        plugins_layout = QVBoxLayout(card_plugins)
        plugins_layout.addWidget(SubtitleLabel(f"Модули ({len(self.plugins)} активно)"))

        if not self.plugins:
            plugins_layout.addWidget(BodyLabel("Поместите модули в папку /plugins"))
        else:
            for plugin in self.plugins:
                plugin_widget = plugin.create_ui(card_plugins)
                if plugin_widget:
                    plugins_layout.addWidget(plugin_widget)

        left_layout.addWidget(card_plugins)
        scroll.setWidget(left_widget)
        main_layout.addWidget(scroll, 3)

        # ----------------- ПРАВАЯ КОЛОНКА (Live Preview) -----------------
        right_card = CardWidget(self)
        right_layout = QVBoxLayout(right_card)
        right_layout.setContentsMargins(15, 15, 15, 15)

        right_layout.addWidget(SubtitleLabel("Предпросмотр 9:16 (Live Preview)"))

        self.canvas = PreviewCanvas()
        self.canvas.set_font_family(self._current_family())
        self.canvas.set_font_size(size_val)
        self.canvas.set_pos(float(s.get("subs_x")), float(s.get("subs_y")))
        self.canvas.set_subs_visible(bool(s.get("subs_enabled")))
        self.canvas.pos_changed.connect(self._on_subs_pos_changed)
        self.canvas.drag_finished.connect(self.settings.save)
        right_layout.addWidget(self.canvas, 0, Qt.AlignmentFlag.AlignCenter)

        self.btn_refresh = PushButton("Обновить кадр")
        self.btn_refresh.clicked.connect(self._update_preview)
        right_layout.addWidget(self.btn_refresh)

        self.progress_bar = ProgressBar()
        self.progress_bar.setValue(0)
        right_layout.addWidget(self.progress_bar)

        self.lbl_status = BodyLabel("Статус: ожидает действий")
        self.lbl_status.setWordWrap(True)
        right_layout.addWidget(self.lbl_status)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(150)
        self.log_view.setMaximumBlockCount(1500)
        self.log_view.setStyleSheet(
            "QPlainTextEdit { background-color: #151515; color: #cfcfcf; border: 1px solid #3c3c3c; "
            "border-radius: 8px; font-family: Consolas, monospace; font-size: 11px; }"
        )
        right_layout.addWidget(self.log_view)
        bus.message.connect(self.log_view.appendPlainText)

        self.btn_start = PrimaryPushButton("Запустить конвейер")
        self.btn_start.setFixedHeight(45)
        self.btn_start.clicked.connect(self._start_render)
        right_layout.addWidget(self.btn_start)

        main_layout.addWidget(right_card, 2)

    # ------------------------------------------------------------------ handlers
    def _title_y_pos(self):
        idx = self.combo_pos.currentIndex()
        if idx == 1:
            return "(h-text_h)/2"
        if idx == 2:
            return "h-300"
        return "200"

    def _schedule_preview(self):
        self._preview_timer.start()

    def _on_seg_changed(self, v):
        self.lbl_seg_info.setText(f"Длительность одной части: {v} минут")
        self.settings.set("seg_minutes", v)

    def _on_blur_changed(self, v):
        self.lbl_blur_info.setText(f"Сила размытия фона: {v} px")
        self.settings.set("blur", v)
        self._schedule_preview()

    def _on_hflip_changed(self, checked):
        self.settings.set("hflip", bool(checked))
        self._schedule_preview()

    def _on_template_changed(self, text):
        self.settings.set("text_template", text)
        self._schedule_preview()

    def _on_title_pos_changed(self, idx):
        self.settings.set("text_pos_index", idx)
        self._schedule_preview()

    def _on_subs_toggle(self, checked):
        self.settings.set("subs_enabled", bool(checked))
        self.canvas.set_subs_visible(bool(checked))

    def _on_font_changed(self, _idx):
        family = self._current_family()
        self.settings.set("subs_font_family", family)
        self.canvas.set_font_family(family)

    def _on_font_size_changed(self, v):
        self.lbl_font_size.setText(f"Размер субтитров: {v}")
        self.settings.set("subs_font_size", v)
        self.canvas.set_font_size(v)

    def _on_subs_pos_changed(self, nx, ny):
        self.settings.set("subs_x", round(nx, 4), save=False)
        self.settings.set("subs_y", round(ny, 4), save=False)

    def _add_fonts(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Выберите шрифты", "", "Fonts (*.ttf *.otf *.ttc)"
        )
        if not paths:
            return
        before = set(self.font_files)
        for path in paths:
            try:
                shutil.copy2(path, os.path.join(FONTS_DIR, os.path.basename(path)))
            except OSError as e:
                InfoBar.error("Ошибка", f"Не удалось скопировать шрифт: {e}",
                              parent=self, position=InfoBarPosition.TOP)
        self._load_fonts()
        new = sorted(set(self.font_files) - before)
        select = new[0] if new else self._current_family()
        self._fill_font_combo(select)
        self._on_font_changed(0)

    def _select_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Выберите видео", "", "Video (*.mp4 *.mkv *.mov *.avi)")
        if path:
            self.input_video = path
            self.lbl_file.setText(os.path.basename(path))
            self._update_preview()

    def _select_dir(self):
        folder = QFileDialog.getExistingDirectory(self, "Папка сохранения")
        if folder:
            self.output_directory = folder
            self.lbl_dir.setText(folder)
            self.settings.set("output_dir", folder)

    def _update_preview(self):
        if not self.input_video or not os.path.exists(self.input_video):
            return

        ok, err = VideoEngine.generate_preview(
            input_file=self.input_video,
            blur=self.slider_blur.value(),
            hflip=self.switch_hflip.isChecked(),
            text_template=self.txt_template.text(),
            output_path=self.preview_cache_file,
            y_pos=self._title_y_pos()
        )

        pixmap = QPixmap()
        if ok and os.path.exists(self.preview_cache_file):
            with open(self.preview_cache_file, "rb") as f:
                pixmap.loadFromData(f.read())

        if ok and not pixmap.isNull():
            self.canvas.set_frame(pixmap)
        else:
            self.canvas.set_frame(None)
            self.canvas.set_message("Ошибка превью")
            self.lbl_status.setText(f"Статус: ошибка превью\n{err}")
            log(f"Ошибка превью:\n{err}")

    def _start_render(self):
        if not self.input_video or not self.output_directory:
            InfoBar.warning(
                "Внимание",
                "Сначала выберите входное видео и папку сохранения!",
                parent=self,
                position=InfoBarPosition.TOP
            )
            return

        self.btn_start.setEnabled(False)
        self.settings.save()
        log(f"Запуск конвейера: шрифт='{self._current_family()}', размер={self.slider_font_size.value()}, "
            f"модель={WHISPER_MODELS[self.combo_model.currentIndex()]}, "
            f"язык={LANG_CODES[self.combo_lang.currentIndex()]}, субтитры={self.switch_subs.isChecked()}")

        self.worker = RenderWorker(
            input_file=self.input_video,
            out_dir=self.output_directory,
            seg_minutes=self.slider_seg.value(),
            blur=self.slider_blur.value(),
            hflip=self.switch_hflip.isChecked(),
            text_tpl=self.txt_template.text(),
            y_pos=self._title_y_pos(),
            plugins=self.plugins,
            subs_enabled=self.switch_subs.isChecked(),
            subs_family=self._current_family() or "Arial",
            subs_size=self.slider_font_size.value(),
            subs_x=float(self.settings.get("subs_x")),
            subs_y=float(self.settings.get("subs_y")),
            whisper_model=WHISPER_MODELS[self.combo_model.currentIndex()],
            language=LANG_CODES[self.combo_lang.currentIndex()]
        )
        self.worker.status_changed.connect(lambda t: self.lbl_status.setText(t))
        self.worker.progress_changed.connect(lambda cur, tot, pct: self.progress_bar.setValue(int(pct)))
        self.worker.finished_all.connect(self._on_finished)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()

    def _on_finished(self):
        self.btn_start.setEnabled(True)
        self.lbl_status.setText("Статус: Готово!")
        InfoBar.success("Успех", "Все серии успешно нарезаны в 9:16!", parent=self, position=InfoBarPosition.TOP)

    def _on_failed(self, error):
        self.btn_start.setEnabled(True)
        self.lbl_status.setText("Статус: Ошибка")
        log(f"ОШИБКА: {error}")
        InfoBar.error("Ошибка рендеринга", error, parent=self, position=InfoBarPosition.TOP, duration=-1)

    def closeEvent(self, event):
        self.settings.save()
        super().closeEvent(event)


def _excepthook(exc_type, exc, tb):
    log("Необработанное исключение:\n" + "".join(traceback.format_exception(exc_type, exc, tb)))


if __name__ == "__main__":
    reset_log()
    sys.excepthook = _excepthook
    app = QApplication(sys.argv)
    setTheme(Theme.DARK)
    window = StudioWindow()
    window.show()
    sys.exit(app.exec())