from PySide6.QtWidgets import QWidget, QVBoxLayout
from qfluentwidgets import CheckBox, Slider, BodyLabel
from core.plugin_base import BasePlugin

class CinemaColorPlugin(BasePlugin):
    name = "Кинематографичный грейдинг"
    description = "Повышает насыщенность и контраст для сочной картинки"
    version = "1.1.0"

    def __init__(self):
        super().__init__()
        self.is_active = False
        self.contrast = 1.15
        self.saturation = 1.25

    def create_ui(self, parent: QWidget) -> QWidget:
        container = QWidget(parent)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(5, 5, 5, 5)

        self.chk_enable = CheckBox("Включить цветокоррекцию (Saturation & Contrast)", container)
        self.chk_enable.stateChanged.connect(self._on_toggle)
        layout.addWidget(self.chk_enable)

        return container

    def _on_toggle(self, state):
        self.is_active = bool(state)

    def modify_filter_graph(self, current_filter: str, segment_index: int, total_segments: int) -> str:
        if self.is_active:
            # Накладываем EQ фильтр
            return current_filter + f",eq=contrast={self.contrast}:saturation={self.saturation}"
        return current_filter