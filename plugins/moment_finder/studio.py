"""
Интерфейс в духе After Effects (v1):

  ┌──────────── timeline: шкала времени, дорожка «Интересность» (кривая),
  │             маркеры моментов, перетаскиваемые in/out-ручки, playhead ┐
  ├─ view: колёса мыши = зум вокруг курсора, ЛКМ по пустому месту = скраб,
  │        ЛКМ на ручке/маркере = перемещение границ/всего момента ───────┤
  ├─ панель моментов: список кандидатов с оценкой и статусом              │
  ├─ превью кадра + транспорт (перемотка по моментам)                     │
  └─ обучение: Подтвердить / Отклонить → дообучение модели; переобучение  ┘

TODO (зарезервировано): система плагинов внутри редактора (панель эффектов,
экспрешны, рендер-очередь) — расширение через MomentStudio.register_tool().
"""
import os
import subprocess
import sys
import tempfile

from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QSplitter, QListWidget, QListWidgetItem,
    QFileDialog, QMessageBox, QFrame
)
from PySide6.QtCore import Qt, Signal, QRectF, QPointF, QSize
from PySide6.QtGui import (
    QPainter, QColor, QPen, QFont, QPolygonF, QImage, QPixmap, QCursor
)
from qfluentwidgets import (
    CardWidget, SubtitleLabel, BodyLabel, StrongBodyLabel, PushButton,
    PrimaryPushButton, TransparentPushButton, ComboBox, LineEdit, SearchLineEdit,
    ProgressBar, InfoBar, InfoBarPosition, FluentIcon, SpinBox, Slider, SwitchButton
)


def _flags():
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


# ====================================================================== TIMELINE
class InterestCurveItem(QWidget):
    """Дорожка кривой интересности (как график аудио-волны в AE)."""
    clicked_at = Signal(float)          # время в секундах (для playhead)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(96)
        self.setSizePolicy(self.sizePolicy().Expanding, self.sizePolicy().Fixed)
        self._curve = []                # [(t, v)] нормализованные точки
        self._duration = 1.0
        self.view = (0.0, 1.0)          # (view_start, view_end) сек — общее с таймлайном

    def set_curve(self, times, curve, duration):
        mx = max(curve) or 1.0
        self._curve = list(zip(times, [c / mx for c in curve]))
        self._duration = max(duration, 0.001)
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor(24, 24, 28))
        if not self._curve:
            p.setPen(QColor(120, 120, 120))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       "Кривая интересности появится после анализа")
            return
        vs, ve = self.view
        xs = lambda t: (t - vs) / max(ve - vs, 1e-6) * w
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(76, 194, 255, 60))
        poly = QPolygonF([QPointF(xs(vs), h)])
        for t, v in self._curve:
            x = xs(t)
            if -5 <= x <= w + 5:
                poly.append(QPointF(x, h - v * (h - 8) - 4))
        poly.append(QPointF(xs(ve), h))
        p.drawPolygon(poly)
        p.setPen(QPen(QColor(76, 194, 255), 1.6))
        line = QPolygonF([pt for pt in poly[1:-1]])
        if len(line) > 1:
            p.drawPolyline(line)


class MomentTimeline(QWidget):
    """Основная таймлайн-дорожка: линейка, playhead, маркеры моментов."""
    seek = Signal(float)                    # перемещение playhead (сек)
    selection_changed = Signal(int)         # индекс выбранного момента
    moment_moved = Signal(int, float, float)  # сдвиг границ/окна

    HANDLE_PX = 7

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(120)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._duration = 1.0
        self.view = [0.0, 1.0]
        self.playhead = 0.0
        self.moments = []               # [{'start','end','status',...}]
        self.selected = -1
        self._drag = None               # ('move'|'left'|'right', idx, off)
        self._scrubbing = False
        self._ruler_h = 26
        self._lane_y0 = self._ruler_h + 30

    # ------------------------------------------------------------- API
    def set_video(self, duration):
        self._duration = max(duration, 0.001)
        self.view = [0.0, self._duration]
        self.update()

    def set_moments(self, moments, selected=-1):
        self.moments = moments
        self.selected = selected
        self.update()

    def set_playhead(self, t):
        self.playhead = max(0.0, min(self._duration, t))
        self._ensure_visible()
        self.update()

    def sync_view(self, other: InterestCurveItem):
        self._curve_item = other
        other.view = tuple(self.view)

    # ------------------------------------------------------------- coords
    def _t2x(self, t):
        vs, ve = self.view
        return (t - vs) / max(ve - vs, 1e-6) * self.width()

    def _x2t(self, x):
        vs, ve = self.view
        return vs + x / max(self.width(), 1) * (ve - vs)

    def _moment_rect(self, m):
        x0 = self._t2x(m["start"])
        x1 = self._t2x(m["end"])
        y0 = self._lane_y0
        return QRectF(x0, y0, max(x1 - x0, 4), 34)

    # ------------------------------------------------------------- paint
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor(30, 30, 34))

        # --- линейка
        p.setPen(QColor(90, 90, 96))
        p.drawLine(0, self._ruler_h, w, self._ruler_h)
        span = self.view[1] - self.view[0]
        step = self._nice_step(span / max(w / 90, 1))
        t = (int(self.view[0] / step) + 1) * step
        f = QFont("Segoe UI", 8)
        p.setFont(f)
        while t < self.view[1]:
            x = self._t2x(t)
            p.setPen(QColor(130, 130, 138))
            p.drawLine(int(x), self._ruler_h - 8, int(x), self._ruler_h)
            mm, ss = divmod(int(t), 60)
            p.drawText(int(x) + 3, 14, f"{mm:02d}:{ss:02d}")
            t += step

        # --- фоновая полоса дорожки
        p.fillRect(0, self._lane_y0 - 4, w, 42, QColor(38, 38, 44))

        # --- моменты
        colors = {
            "pending": (QColor(76, 194, 255, 90), QColor(76, 194, 255)),
            "accepted": (QColor(108, 222, 120, 110), QColor(108, 222, 120)),
            "rejected": (QColor(232, 96, 96, 70), QColor(232, 96, 96)),
        }
        for i, m in enumerate(self.moments):
            r = self._moment_rect(m)
            fill, line = colors.get(m.get("status", "pending"), colors["pending"])
            p.setPen(QPen(line, 2.2 if i == self.selected else 1.2))
            p.setBrush(fill)
            rr = QRectF(r.x(), r.y(), r.width(), r.height())
            p.drawRoundedRect(rr, 5, 5)
            # ручки in/out
            p.setBrush(line)
            p.setPen(Qt.PenStyle.NoPen)
            for hx in (r.x(), r.right()):
                p.drawRoundedRect(QRectF(hx - 2.5, r.y() + 8, 5, 18), 2, 2)
            # подпись
            p.setPen(QColor(240, 240, 240))
            label = f"{m['peak']:.2f}"
            if m.get("status") != "pending":
                label = ("✓ " if m["status"] == "accepted" else "✗ ") + label
            if r.width() > 34:
                p.drawText(r.adjusted(8, 0, -8, 0),
                           Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, label)

        # --- playhead
        px = self._t2x(self.playhead)
        p.setPen(QPen(QColor(255, 214, 89), 1.6))
        p.drawLine(int(px), 0, int(px), h)
        p.setBrush(QColor(255, 214, 89))
        p.setPen(Qt.PenStyle.NoPen)
        tri = QPolygonF([QPointF(px - 6, 0), QPointF(px + 6, 0), QPointF(px, 9)])
        p.drawPolygon(tri)

    @staticmethod
    def _nice_step(raw):
        for base in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900):
            if raw <= base:
                return base
        return 1800

    # ------------------------------------------------------------- mouse
    def _hit(self, pos):
        x, y = pos.x(), pos.y()
        if self._lane_y0 - 6 <= y <= self._lane_y0 + 40:
            for i, m in enumerate(self.moments):
                r = self._moment_rect(m)
                if abs(x - r.x()) <= self.HANDLE_PX:
                    return ("left", i)
                if abs(x - r.right()) <= self.HANDLE_PX:
                    return ("right", i)
                if r.contains(QPointF(pos)):
                    return ("body", i)
        return (None, -1)

    def mousePressEvent(self, ev):
        kind, i = self._hit(ev.position())
        if kind == "body":
            self.selected = i
            self.selection_changed.emit(i)
            self._drag = ("move", i, self._x2t(ev.x()) - self.moments[i]["start"])
        elif kind in ("left", "right"):
            self.selected = i
            self.selection_changed.emit(i)
            self._drag = (kind, i, 0.0)
        else:
            self._scrubbing = True
            self.seek.emit(max(0.0, min(self._duration, self._x2t(ev.x()))))
        self.update()

    def mouseMoveEvent(self, ev):
        if self._drag:
            kind, i, off = self._drag
            m = self.moments[i]
            t = self._x2t(ev.x())
            if kind == "move":
                dur = m["end"] - m["start"]
                s = max(0.0, min(self._duration - dur, t - off))
                self.moment_moved.emit(i, round(s, 2), round(s + dur, 2))
            elif kind == "left":
                self.moment_moved.emit(i, round(max(0.0, min(t, m["end"] - 1)), 2), m["end"])
            else:
                self.moment_moved.emit(i, m["start"],
                                       round(min(self._duration, max(t, m["start"] + 1)), 2))
        elif self._scrubbing:
            self.seek.emit(max(0.0, min(self._duration, self._x2t(ev.x()))))
        else:
            kind, i = self._hit(ev.position())
            if kind in ("left", "right"):
                self.setCursor(QCursor(Qt.CursorShape.SizeHorCursor))
            elif kind == "body":
                self.setCursor(QCursor(Qt.CursorShape.SizeAllCursor))
            else:
                self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))

    def mouseReleaseEvent(self, ev):
        self._drag = None
        self._scrubbing = False

    def wheelEvent(self, ev):
        up = ev.angleDelta().y() > 0
        anchor = self._x2t(ev.position().x())
        vs, ve = self.view
        span = ve - vs
        new = span * (0.8 if up else 1.25)
        new = max(min(new, self._duration), 5.0)
        frac = (anchor - vs) / max(span, 1e-6)
        s2 = max(0.0, anchor - frac * new)
        e2 = min(self._duration, s2 + new)
        s2 = max(0.0, e2 - new)
        self.view = [s2, e2]
        self._propagate_view()
        self.update()

    def _ensure_visible(self):
        vs, ve = self.view
        if not (vs <= self.playhead <= ve):
            span = ve - vs
            self.view = [max(0.0, self.playhead - span / 2),
                         min(self._duration, self.playhead + span / 2)]
            self._propagate_view()

    def _propagate_view(self):
        ci = getattr(self, "_curve_item", None)
        if ci is not None:
            ci.view = tuple(self.view)
            ci.update()


# ====================================================================== WAVEFORM-LIKE PREVIEW
class FramePreview(CardWidget):
    def __init__(self, parent=None, workdir=None):
        super().__init__(parent)
        self.workdir = workdir or tempfile.gettempdir()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        self.title = StrongBodyLabel("Превью кадра", self)
        lay.addWidget(self.title)
        self.canvas = QWidget(self)
        self.canvas.setMinimumHeight(240)
        self._pix = None
        self._msg = "Выберите момент для просмотра кадра"
        lay.addWidget(self.canvas, 1)
        self.canvas.paintEvent = self._paint_canvas

    def _paint_canvas(self, ev):
        p = QPainter(self.canvas)
        rect = self.canvas.rect()
        p.fillRect(rect, QColor(18, 18, 20))
        if self._pix and not self._pix.isNull():
            scaled = self._pix.scaled(self.canvas.size(),
                                      Qt.AspectRatioMode.KeepAspectRatio,
                                      Qt.TransformationMode.SmoothTransformation)
            x = (rect.width() - scaled.width()) // 2
            y = (rect.height() - scaled.height()) // 2
            p.drawPixmap(x, y, scaled)
        else:
            p.setPen(QColor(140, 140, 140))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._msg)

    def show_frame(self, path):
        try:
            img = QImage(path)
            if img.isNull():
                raise ValueError
            self._pix = QPixmap.fromImage(img)
        except Exception:
            self._pix = None
        self.canvas.update()

    def show_message(self, msg):
        self._msg = msg
        self._pix = None
        self.canvas.update()


# ====================================================================== MAIN WINDOW
class MomentStudio(QWidget):
    """Отдельное окно редактора моментов."""
    request_cut = Signal(list)      # список подтверждённых моментов → резать

    def __init__(self, plugin, video_path: str, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.plugin = plugin
        self.video_path = video_path
        self.setWindowTitle(f"Редактор моментов — {os.path.basename(video_path)}")
        self.resize(1280, 820)
        self.feats = None
        self.curve = []
        self.duration = 0.0
        self.moments = []
        self._frame_job = None
        self._build_ui()
        if plugin.features_ready(video_path):
            self._load_existing_analysis()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        head = QHBoxLayout()
        head.addWidget(SubtitleLabel("Монтажная студия моментов"))
        head.addStretch(1)
        self.btn_analyze = PrimaryPushButton(FluentIcon.PLAY, "Анализировать видео", self)
        self.btn_analyze.clicked.connect(self._analyze)
        head.addWidget(self.btn_analyze)
        self.btn_retrain = PushButton(FluentIcon.SYNC, "Переобучить на всём опыте", self)
        self.btn_retrain.clicked.connect(self._retrain)
        head.addWidget(self.btn_retrain)
        self.btn_cut = TransparentPushButton(FluentIcon.CUT, "Нарезать подтверждённые", self)
        self.btn_cut.clicked.connect(self._cut_accepted)
        head.addWidget(self.btn_cut)
        root.addLayout(head)

        self.progress = ProgressBar(self)
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        splitter = QSplitter(Qt.Orientation.Vertical, self)

        # ------- верх: список моментов + превью
        top = QHBoxLayout()
        left_panel = CardWidget(splitter)
        lp = QVBoxLayout(left_panel)
        lp.addWidget(StrongBodyLabel("Найденные моменты"))
        self.lbl_model = BodyLabel("Модель: не обучена (эвристический скор)", self)
        lp.addWidget(self.lbl_model)
        self.list = QListWidget(left_panel)
        self.list.currentRowChanged.connect(self._on_select)
        lp.addWidget(self.list, 1)
        btns = QHBoxLayout()
        self.btn_accept = PushButton(FluentIcon.ACCEPT, "Подтвердить", self)
        self.btn_accept.clicked.connect(lambda: self._feedback(True))
        self.btn_reject = PushButton(FluentIcon.CANCEL, "Отклонить", self)
        self.btn_reject.clicked.connect(lambda: self._feedback(False))
        self.btn_reset = TransparentPushButton("Сбросить статус", self)
        self.btn_reset.clicked.connect(lambda: self._feedback(None))
        btns.addWidget(self.btn_accept)
        btns.addWidget(self.btn_reject)
        btns.addWidget(self.btn_reset)
        lp.addLayout(btns)
        top.addWidget(left_panel, 2)

        self.preview = FramePreview(splitter)
        top.addWidget(self.preview, 3)
        splitter.addWidget(self._wrap(top))

        # ------- низ: таймлайн + кривая
        bottom = QVBoxLayout()
        tl_card = CardWidget(splitter)
        tc = QVBoxLayout(tl_card)
        tc.setContentsMargins(8, 8, 8, 8)
        tc.setSpacing(2)
        self.timeline = MomentTimeline(tl_card)
        self.timeline.seek.connect(self._seek_to)
        self.timeline.selection_changed.connect(lambda i: self.list.setCurrentRow(i))
        self.timeline.moment_moved.connect(self._moment_moved)
        self.curve_item = InterestCurveItem(tl_card)
        self.timeline.sync_view(self.curve_item)
        tc.addWidget(self.timeline)
        tc.addWidget(BodyLabel("Дорожка «Интересность»"))
        tc.addWidget(self.curve_item)
        bottom.addWidget(tl_card)

        tools = QHBoxLayout()
        tools.addWidget(BodyLabel("Порог:"))
        self.spin_thr = DoubleSpinBoxCompat(self)
        self.spin_thr.valueChanged.connect(lambda _: self._recompute())
        tools.addWidget(self.spin_thr)
        tools.addWidget(BodyLabel("Мин. длина, с:"))
        self.spin_min = SpinBox(self)
        self.spin_min.setRange(5, 120)
        self.spin_min.setValue(int(self.plugin.params.get("min_len", 15)))
        self.spin_min.valueChanged.connect(lambda _: self._recompute())
        tools.addWidget(self.spin_min)
        tools.addWidget(BodyLabel("Макс. длина, с:"))
        self.spin_max = SpinBox(self)
        self.spin_max.setRange(10, 300)
        self.spin_max.setValue(int(self.plugin.params.get("max_len", 60)))
        self.spin_max.valueChanged.connect(lambda _: self._recompute())
        tools.addWidget(self.spin_max)
        tools.addWidget(BodyLabel("Топ:"))
        self.spin_top = SpinBox(self)
        self.spin_top.setRange(1, 60)
        self.spin_top.setValue(int(self.plugin.params.get("top_k", 12)))
        self.spin_top.valueChanged.connect(lambda _: self._recompute())
        tools.addWidget(self.spin_top)
        tools.addStretch(1)
        tools.addWidget(BodyLabel("ЛКМ — скраб · колесо — зум · тяни ручки момента — сдвинуть границу"))
        bottom.addLayout(tools)
        splitter.addWidget(self._wrap(bottom))
        splitter.setSizes([380, 420])
        root.addWidget(splitter, 1)

    @staticmethod
    def _wrap(layout):
        w = QWidget()
        w.setLayout(layout)
        return w

    # ------------------------------------------------------------------ analysis
    def _analyze(self):
        if self.plugin.is_analyzing():
            InfoBar.warning("Занято", "Анализ уже выполняется", parent=self,
                            position=InfoBarPosition.TOP)
            return
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self.btn_analyze.setEnabled(False)
        self.plugin.analyze(self.video_path, self._on_progress, self._on_features)

    def _on_progress(self, pct, stage):
        self.progress.setValue(int(pct))
        self.lbl_model.setText(f"Модель: {stage}…")

    def _on_features(self, feats):
        self.btn_analyze.setEnabled(True)
        self.progress.setVisible(False)
        self.feats = feats
        self.duration = feats["duration"]
        self.timeline.set_video(self.duration)
        from .scorer import gaussian_smooth
        self.curve = gaussian_smooth(feats["motion"], 2.0)
        self._recompute()
        self._refresh_model_label()

    def _load_existing_analysis(self):
        feats = plugin_features = self.plugin.get_cached_features(self.video_path)
        if feats:
            self._on_features(feats)

    def _recompute(self):
        if not self.feats:
            return
        params = {"min_len": self.spin_min.value(), "max_len": self.spin_max.value(),
                  "threshold": self.spin_thr.value(), "top_k": self.spin_top.value()}
        self.plugin.params.update(params)
        self.plugin.save_params()
        moments = self.plugin.find_moments(self.video_path, self.feats, params)
        # сохраняем прежние статусы по совпадению границ
        old = {(m["start"], m["end"]): m.get("status", "pending") for m in self.moments}
        for m in moments:
            m.setdefault("status", old.get((m["start"], m["end"]), "pending"))
        self.moments = moments
        self._fill_list()
        self.timeline.set_moments(self.moments, self.list.currentRow())
        # кривая — скор моделью
        from .model import vectorize
        model = self.plugin.model
        if model.w is not None and model.n_samples > 0:
            sc = [model.predict(vectorize(self.feats, i)) for i in range(len(self.feats["times"]))]
        else:
            from .scorer import heuristic_score
            import numpy as np
            sc = heuristic_score(self.feats).tolist()
        self.curve_item.set_curve(self.feats["times"], sc, self.duration)
        self.curve_item.view = tuple(self.timeline.view)
        self._refresh_model_label()

    def _retrain(self):
        n = self.plugin.retrain_from_feedback()
        if n == 0:
            InfoBar.info("Опыт пуст", "Пока нет подтверждений/отклонений для обучения.",
                         parent=self, position=InfoBarPosition.TOP)
            return
        InfoBar.success("Переобучено", f"Модель обновлена на {n} примерах.",
                        parent=self, position=InfoBarPosition.TOP)
        self._recompute()

    def _refresh_model_label(self):
        rep = self.plugin.model.weights_report()
        if rep:
            parts = ", ".join(f"{k}:{v:+.2f}" for k, v in rep[:4])
            self.lbl_model.setText(
                f"Модель обучена (примеров: {self.plugin.model.n_samples}). Веса: {parts}")
        else:
            self.lbl_model.setText("Модель: не обучена (эвристический скор)")

    # ------------------------------------------------------------------ list
    def _fill_list(self):
        self.list.blockSignals(True)
        self.list.clear()
        for m in self.moments:
            st = {"pending": "○", "accepted": "✓", "rejected": "✗"}[m.get("status", "pending")]
            mm1, ss1 = divmod(int(m["start"]), 60)
            mm2, ss2 = divmod(int(m["end"]), 60)
            item = QListWidgetItem(
                f"{st}  {mm1:02d}:{ss1:02d}–{mm2:02d}:{ss2:02d}   ({m['end']-m['start']:.0f} c)  "
                f"скор {m['peak']:.2f}")
            if m.get("status") == "accepted":
                item.setBackground(QColor(38, 70, 45))
            elif m.get("status") == "rejected":
                item.setBackground(QColor(80, 40, 40))
            self.list.addItem(item)
        self.list.blockSignals(False)
        if self.moments:
            self.list.setCurrentRow(0)

    def _on_select(self, row):
        if 0 <= row < len(self.moments):
            m = self.moments[row]
            mid = (m["start"] + m["end"]) / 2
            self.timeline.set_playhead(mid)
            self.timeline.selected = row
            self.timeline.update()
            self._grab_frame(mid)

    def _moment_moved(self, idx, start, end):
        if 0 <= idx < len(self.moments):
            self.moments[idx]["start"] = start
            self.moments[idx]["end"] = end
            self._fill_list()
            self.timeline.set_moments(self.moments, idx)

    # ------------------------------------------------------------------ frames
    def _seek_to(self, t):
        self.timeline.set_playhead(t)
        self._grab_frame(t)

    def _grab_frame(self, t):
        out = os.path.join(self.plugin.cache_dir, "studio_frame.jpg")
        try:
            kw = dict(capture_output=True, timeout=30)
            if sys.platform == "win32":
                kw["creationflags"] = subprocess.CREATE_NO_WINDOW
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", self.video_path,
                 "-frames:v", "1", "-vf", "scale=480:-2", out], **kw)
            if os.path.exists(out):
                self.preview.show_frame(out)
                mm, ss = divmod(int(t), 60)
                self.preview.title.setText(f"Превью кадра — {mm:02d}:{ss:02d}")
        except Exception:
            self.preview.show_message("Не удалось извлечь кадр")

    # ------------------------------------------------------------------ feedback
    def _feedback(self, accept):
        row = self.list.currentRow()
        if not (0 <= row < len(self.moments)) or not self.feats:
            return
        m = self.moments[row]
        m["status"] = "accepted" if accept is True else ("rejected" if accept is False else "pending")
        if accept is not None:
            from .model import vectorize
            i = min(range(len(self.feats["times"])), key=lambda j: abs(self.feats["times"][j] - m["start"]))
            x = vectorize(self.feats, i)
            self.plugin.learn_from(m, x, bool(accept), self.video_path)
            InfoBar.success(
                "Обучение",
                "Момент подтверждён — модель запомнила критерии." if accept
                else "Момент отклонён — модель учла это.",
                parent=self, position=InfoBarPosition.TOP, duration=2500)
        self._fill_list()
        self.list.setCurrentRow(row)
        self.timeline.set_moments(self.moments, row)
        self._refresh_model_label()

    # ------------------------------------------------------------------ cut
    def _cut_accepted(self):
        acc = [m for m in self.moments if m.get("status") == "accepted"]
        if not acc:
            InfoBar.warning("Пусто", "Сначала подтвердите хотя бы один момент (✓).",
                            parent=self, position=InfoBarPosition.TOP)
            return
        self.request_cut.emit(acc)


class DoubleSpinBoxCompat(QWidget):
    """Мини-спинбокс с шагом 0.05 без внешних зависимостей."""
    valueChanged = Signal(float)

    def __init__(self, parent=None, lo=0.05, hi=0.95, val=0.45):
        super().__init__(parent)
        self.lo, self.hi, self._v = lo, hi, val
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 0, 2, 0)
        lay.setSpacing(2)
        self.edit = LineEdit(self)
        self.edit.setFixedWidth(58)
        self.edit.setText(f"{val:.2f}")
        self.edit.textChanged.connect(self._on_text)
        lay.addWidget(self.edit)

    def value(self):
        return self._v

    def setValue(self, v):
        self._v = max(self.lo, min(self.hi, v))
        self.edit.setText(f"{self._v:.2f}")

    def _on_text(self, txt):
        try:
            v = float(txt.replace(",", "."))
        except ValueError:
            return
        nv = max(self.lo, min(self.hi, v))
        if nv != self._v:
            self._v = nv
            self.valueChanged.emit(nv)
