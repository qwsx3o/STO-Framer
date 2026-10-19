import os
import time
import traceback
from PySide6.QtCore import QObject, Signal
from core.settings import CACHE_DIR

LOG_PATH = os.path.join(CACHE_DIR, "app.log")


class _LogBus(QObject):
    message = Signal(str)


bus = _LogBus()


def reset_log():
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(LOG_PATH, "w", encoding="utf-8"):
            pass
    except OSError:
        pass


def log(text: str):
    """Потокобезопасно: пишет в cache/app.log и в панель лога окна."""
    line = f"[{time.strftime('%H:%M:%S')}] {text}"
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    bus.message.emit(line)


def log_exception(prefix: str = ""):
    log(f"{prefix}\n{traceback.format_exc()}".strip())