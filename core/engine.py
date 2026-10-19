import os
import sys
import subprocess
import json
from typing import Optional, Tuple


def _flags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _parse_fps(value: str) -> float:
    try:
        if "/" in value:
            num, den = value.split("/")
            den = float(den)
            return float(num) / den if den else 30.0
        return float(value)
    except (ValueError, ZeroDivisionError):
        return 30.0


class VideoEngine:
    @staticmethod
    def get_system_font() -> str:
        """Находит стандартный системный шрифт с поддержкой кириллицы

        и форматирует путь под синтаксис фильтров FFmpeg.
        """
        windir = os.environ.get("WINDIR", "C:\\Windows")
        candidates = ["arial.ttf", "calibri.ttf", "segoeui.ttf"]

        for font_name in candidates:
            path = os.path.join(windir, "Fonts", font_name)
            if os.path.exists(path):
                # FFmpeg требует прямые слеши и экранирование двоеточия (C\:/...)
                return path.replace("\\", "/").replace(":", r"\:")
        return ""

    @staticmethod
    def inspect_file(filepath: str) -> dict:
        cmd = [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            filepath
        ]
        res = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=_flags()
        )
        data = json.loads(res.stdout)

        v_stream = next(s for s in data.get("streams", []) if s.get("codec_type") == "video")
        duration = float(data.get("format", {}).get("duration", 0) or v_stream.get("duration", 0))

        return {
            "duration": duration,
            "width": int(v_stream.get("width", 0)),
            "height": int(v_stream.get("height", 0)),
            "fps": _parse_fps(v_stream.get("r_frame_rate", "30/1"))
        }

    @staticmethod
    def build_filter(blur: int, hflip: bool, text_template: str, part: int, total: int,
                     y_pos: str, ass_file: Optional[str] = None) -> str:
        """ass_file — путь относительно рабочей директории FFmpeg (cwd = APP_DIR), без двоеточий."""
        text = text_template.replace("{part}", str(part)).replace("{total}", str(total))
        # Экранирование двоеточия и кавычек для синтаксиса drawtext
        clean_text = text.replace(":", r"\:").replace("'", "")

        # scale=1080:-2 гарантирует четную высоту для совместимости с кодеком yuv420p
        fg_filter = "scale=1080:-2,hflip" if hflip else "scale=1080:-2"
        bg_filter = f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur={blur}:5"
        if hflip:
            bg_filter += ",hflip"

        font_path = VideoEngine.get_system_font()
        font_opt = f"fontfile='{font_path}':" if font_path else ""

        filter_graph = (
            f"[0:v]{bg_filter}[bg];"
            f"[0:v]{fg_filter}[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2[base];"
            f"[base]drawtext={font_opt}text='{clean_text}':fontcolor=white:fontsize=64:"
            f"box=1:boxcolor=black@0.45:boxborderw=15:x=(w-text_w)/2:y={y_pos}"
        )
        if ass_file:
            filter_graph += f",ass={ass_file}:fontsdir=fonts"
        return filter_graph

    @staticmethod
    def generate_preview(input_file: str, blur: int, hflip: bool, text_template: str,
                         output_path: str, y_pos: str = "200") -> Tuple[bool, str]:
        try:
            duration = VideoEngine.inspect_file(input_file)["duration"]
        except Exception:
            duration = 0.0
        ss = min(8.0, duration / 2) if duration > 0 else 0.0

        graph = VideoEngine.build_filter(
            blur=blur,
            hflip=hflip,
            text_template=text_template,
            part=1,
            total=10,
            y_pos=y_pos
        )

        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass

        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{ss:.2f}",
            "-i", input_file,
            "-filter_complex", graph,
            "-frames:v", "1",
            "-q:v", "2",
            output_path
        ]
        res = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=_flags()
        )
        if res.returncode != 0 or not os.path.exists(output_path):
            tail = "\n".join((res.stderr or "").strip().splitlines()[-3:])
            return False, tail or "ffmpeg не вернул кадр"
        return True, ""