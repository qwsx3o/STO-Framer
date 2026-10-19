import os
import subprocess
import sys
import time
from typing import List, Tuple

from core.logger import log

Word = Tuple[float, float, str]


def _flags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def extract_audio(input_file: str, start: float, dur: float, out_wav: str) -> bool:
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start),
        "-i", input_file,
        "-t", str(dur),
        "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le",
        out_wav
    ]
    res = subprocess.run(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=_flags()
    )
    return res.returncode == 0


def _register_cuda_dlls():
    """Windows: подключает DLL из pip-пакетов nvidia-* (cublas, cudnn) к поиску библиотек."""
    if sys.platform != "win32":
        return
    added = []
    for base in list(sys.path):
        nv = os.path.join(base, "nvidia")
        if not os.path.isdir(nv):
            continue
        for pkg in os.listdir(nv):
            bin_dir = os.path.join(nv, pkg, "bin")
            if os.path.isdir(bin_dir) and bin_dir not in added:
                added.append(bin_dir)
                try:
                    os.add_dll_directory(bin_dir)
                except OSError:
                    pass
                os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
    if added:
        log("CUDA DLL подключены: " + ", ".join(os.path.basename(os.path.dirname(a)) for a in added))


class Transcriber:
    def __init__(self, model_name: str):
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            raise RuntimeError("Не установлен faster-whisper. Выполните: pip install faster-whisper")
        self._WhisperModel = WhisperModel
        self.model_name = model_name
        self.device = "?"
        _register_cuda_dlls()
        self._load("auto", "auto")

    def _load(self, device: str, compute_type: str):
        log(f"Загрузка модели Whisper '{self.model_name}' (device={device}). "
            f"При первом запуске идёт скачивание, это может занять несколько минут...")
        t0 = time.time()
        self.model = self._WhisperModel(self.model_name, device=device, compute_type=compute_type)
        try:
            self.device = str(self.model.model.device)
        except Exception:
            self.device = device
        log(f"Модель загружена за {time.time() - t0:.1f} с, устройство: {self.device}")

    @staticmethod
    def _load_wav(wav_path: str):
        """Читает 16 кГц mono PCM16 напрямую, без PyAV (обход конфликта версий av)."""
        import wave
        import numpy as np
        with wave.open(wav_path, "rb") as wf:
            raw = wf.readframes(wf.getnframes())
        return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

    def _run(self, audio, total, language, progress_cb) -> List[Word]:
        segments, info = self.model.transcribe(
            audio,
            language=None if language == "auto" else language,
            word_timestamps=True,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            beam_size=1,
            best_of=1,
            temperature=0.0,
            condition_on_previous_text=False
        )
        log(f"Аудио {total:.0f} с, язык: {info.language} ({info.language_probability:.0%})")
        words: List[Word] = []
        for seg in segments:
            for w in (seg.words or []):
                text = w.word.strip()
                if text:
                    words.append((float(w.start), float(w.end), text))
            if progress_cb and total > 0:
                progress_cb(min(100.0, seg.end / total * 100.0), seg.text.strip())
        return words

    def transcribe(self, wav_path: str, language: str = "auto", progress_cb=None) -> List[Word]:
        audio = self._load_wav(wav_path)
        total = len(audio) / 16000.0
        try:
            return self._run(audio, total, language, progress_cb)
        except RuntimeError as e:
            msg = str(e).lower()
            if self.device.startswith("cuda") and any(k in msg for k in ("cublas", "cudnn", "cuda", ".dll")):
                log(f"CUDA недоступна ({e}). Переключаюсь на CPU (int8) — будет медленнее. "
                    f"Для GPU: uv pip install nvidia-cublas-cu12 nvidia-cudnn-cu12")
                self._load("cpu", "int8")
                return self._run(audio, total, language, progress_cb)
            raise


def chunk_words(words: List[Word], max_words: int = 4, max_chars: int = 26, max_gap: float = 0.7):
    chunks = []
    cur: List[Word] = []

    def flush():
        if cur:
            chunks.append((cur[0][0], cur[-1][1], " ".join(w[2] for w in cur)))
            cur.clear()

    for w in words:
        if cur:
            gap = w[0] - cur[-1][1]
            length = len(" ".join(x[2] for x in cur)) + 1 + len(w[2])
            if len(cur) >= max_words or length > max_chars or gap > max_gap:
                flush()
        cur.append(w)
    flush()

    for i, (s, e, t) in enumerate(chunks):
        if e - s < 0.25:
            e = s + 0.25
        if i + 1 < len(chunks):
            e = min(e, chunks[i + 1][0])
        chunks[i] = (s, max(e, s + 0.05), t)
    return chunks


def _ass_time(t: float) -> str:
    cs = int(round(max(0.0, t) * 100))
    h = cs // 360000
    cs %= 360000
    m = cs // 6000
    cs %= 6000
    s = cs // 100
    cs %= 100
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def write_ass(chunks, path: str, family: str, size: int, nx: float, ny: float):
    x = int(round(nx * 1080))
    y = int(round(ny * 1920))
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: 1080",
        "PlayResY: 1920",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{family},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,"
        "0,0,0,0,100,100,0,0,1,5,0,5,40,40,40,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for start, end, text in chunks:
        safe = text.replace("{", "(").replace("}", ")").replace("\n", " ")
        lines.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,"
            f"{{\\an5\\pos({x},{y})}}{safe}"
        )
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(lines) + "\n")