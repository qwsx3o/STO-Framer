import os
import sys
import importlib
import inspect
from typing import List
from core.plugin_base import BasePlugin

class PluginManager:
    def __init__(self, plugins_dir: str = "plugins"):
        self.plugins_dir = os.path.abspath(plugins_dir)
        self.loaded_plugins: List[BasePlugin] = []

    def reload_plugins(self) -> List[BasePlugin]:
        self.loaded_plugins.clear()
        if not os.path.exists(self.plugins_dir):
            os.makedirs(self.plugins_dir, exist_ok=True)
            return self.loaded_plugins

        if self.plugins_dir not in sys.path:
            sys.path.insert(0, self.plugins_dir)

        for entry in os.listdir(self.plugins_dir):
            full_path = os.path.join(self.plugins_dir, entry)
            mod_name = None

            if os.path.isfile(full_path) and entry.endswith(".py") and not entry.startswith("__"):
                mod_name = entry[:-3]
            elif os.path.isdir(full_path) and not entry.startswith("__"):
                if os.path.exists(os.path.join(full_path, "__init__.py")):
                    mod_name = entry

            if mod_name:
                try:
                    if mod_name in sys.modules:
                        mod = importlib.reload(sys.modules[mod_name])
                    else:
                        mod = importlib.import_module(mod_name)

                    for _, obj in inspect.getmembers(mod, inspect.isclass):
                        if issubclass(obj, BasePlugin) and obj is not BasePlugin:
                            instance = obj()
                            self.loaded_plugins.append(instance)
                except Exception as e:
                    print(f"[PluginManager] Ошибка загрузки модуля '{mod_name}': {e}")

        return self.loaded_plugins