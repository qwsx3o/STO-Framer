import json
import os

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONTS_DIR = os.path.join(APP_DIR, "fonts")
CACHE_DIR = os.path.join(APP_DIR, "cache")
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")

DEFAULTS = {
    "seg_minutes": 5,
    "blur": 30,
    "hflip": True,
    "text_template": "Часть {part}",
    "text_pos_index": 0,
    "output_dir": "",
    "subs_enabled": True,
    "subs_font_family": "",
    "subs_font_size": 72,
    "subs_x": 0.5,
    "subs_y": 0.75,
    "whisper_model": "small",
    "subs_language": "auto",
}


class Settings:
    def __init__(self):
        self.data = dict(DEFAULTS)
        self.load()

    def load(self):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for key, value in raw.items():
                if key in DEFAULTS:
                    self.data[key] = value
        except (OSError, ValueError):
            pass

    def get(self, key):
        return self.data.get(key, DEFAULTS.get(key))

    def set(self, key, value, save=True):
        self.data[key] = value
        if save:
            self.save()

    def save(self):
        try:
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except OSError:
            pass