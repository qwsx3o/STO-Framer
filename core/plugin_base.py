from abc import ABC, abstractmethod
from typing import Optional
from PySide6.QtWidgets import QWidget

class BasePlugin(ABC):
    """
    Интерфейс для подключаемых модулей.
    Любая папка или файл в /plugins с этим классом подключится на лету.
    """
    name: str = "Unnamed Module"
    description: str = "Модуль для обработки видео"
    version: str = "1.0.0"

    def __init__(self):
        self.enabled: bool = True

    @abstractmethod
    def create_ui(self, parent: QWidget) -> Optional[QWidget]:
        """Возвращает скомпонованный Qt-виджет настроек для панели управления."""
        pass

    def modify_filter_graph(self, current_filter: str, segment_index: int, total_segments: int) -> str:
        """Позволяет добавить свои FFmpeg-фильтры в пайплайн обработки кадра."""
        return current_filter