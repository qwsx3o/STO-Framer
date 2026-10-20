"""
Съёмка признаков (feature extraction) из видео.

Без готовых нейросетей: сигнальные признаки считаются FFmpeg + NumPy.
Каждый признак нормализуется в [0..1] по перцентилям внутри видео,
чтобы веса обучаемой модели были сравнимы между разными роликами.

Признаки на окно длительностью window секунд:
  motion      — средний absdiff между соседними кадрами (экшн/жесты)
  brightness  — средняя яркость
  cut         — всплеск absdiff относительно локальной медианы (смена ракурса)
  rms         — громкость аудио (RMS)
  speech      — доля энергии в голосовой полосе 80–3500 Гц
  pitch_var   — скачки центра тяжести спектра (интонация, восклицания)
  tempo       — разброс motion вокруг окна (частота изменений картинки)
"""
import os
import sys
import json
import tempfile
import subprocess

import numpy as np


def _flags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _run(cmd, **kw):
    """subprocess.run с CREATE_NO_WINDOW только на Windows."""
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(cmd, **kw)


def probe(path: str) -> dict:
    res = _run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_format", "-show_streams", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if res.returncode != 0:
        raise RuntimeError(res.stderr[-400:] or "ffprobe failed")
    return json.loads(res.stdout or "{}")


def video_duration(path: str) -> float:
    data = probe(path)
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    dur = float(data.get("format", {}).get("duration", 0) or 0)
    if not dur and v is not None:
        try:
            dur = float(v.get("duration", 0) or 0)
        except (TypeError, ValueError):
            dur = 0.0
    return dur


def _normalize(x: np.ndarray) -> np.ndarray:
    """Масштабирует ряд в [0..1] по 5-й и 95-й перцентилям ненулевых значений."""
    pos = x[x > 0]
    if pos.size < 8:
        return np.zeros_like(x)
    lo, hi = np.percentile(pos, 5), np.percentile(pos, 95)
    if hi - lo < 1e-6:
        return np.zeros_like(x)
    return np.clip((x - lo) / (hi - lo), 0.0, 1.0)


class FeatureExtractor:
    """Извлекает временные ряды признаков с шагом `window` секунд."""

    KEYS = ("motion", "brightness", "cut", "rms", "speech", "pitch_var", "tempo")

    def __init__(self, ffmpeg_bin: str = "ffmpeg"):
        self.ffmpeg = ffmpeg_bin

    # ------------------------------------------------------------------ video
    def sample_video(self, path, fps, width=160, height=90, max_frames=3600,
                     progress=None):
        cmd = [self.ffmpeg, "-v", "error", "-i", path,
               "-vf", f"fps={fps},scale={width}:{height}",
               "-frames:v", str(max_frames),
               "-pix_fmt", "gray", "-f", "rawvideo", "-"]
        kw = {}
        if sys.platform == "win32":
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, **kw)
        raw = bytearray()
        frame_size = width * height
        while True:
            chunk = proc.stdout.read(frame_size * 64)
            if not chunk:
                break
            raw.extend(chunk)
            if progress:
                progress(min(1.0, len(raw) / (frame_size * max_frames)))
        proc.wait()
        usable = (len(raw) // frame_size) * frame_size
        arr = np.frombuffer(bytes(raw[:usable]), dtype=np.uint8).reshape(-1, height, width)
        ts = np.arange(len(arr), dtype=np.float64) / fps
        return arr, ts

    # ------------------------------------------------------------------ audio
    def sample_audio(self, path, sr=8000, progress=None):
        tmp = tempfile.NamedTemporaryFile(suffix=".s16le", delete=False)
        tmp.close()
        try:
            cmd = [self.ffmpeg, "-v", "error", "-y", "-i", path,
                   "-ac", "1", "-ar", str(sr), "-f", "s16le", tmp.name]
            r = _run(cmd, capture_output=True, text=True)
            size = os.path.getsize(tmp.name)
            if size == 0:
                # файла может не быть вовсе (без аудиодорожки) — это допустимо
                if progress:
                    progress(1.0)
                return np.zeros(0, np.float32), sr
            data = np.fromfile(tmp.name, dtype="<i2").astype(np.float32) / 32768.0
            if progress:
                progress(1.0)
            return data, sr
        finally:
            try:
                os.remove(tmp.name)
            except OSError:
                pass

    # ------------------------------------------------------------------ main
    def extract(self, path: str, window: float = 1.0, sample_fps: float = 2.0,
                progress=None) -> dict:
        """Возвращает {'times': [...], 'motion': [...], ...} — по одному значению
        на каждое окно длительностью `window` секунд."""
        duration = video_duration(path)
        if duration <= 0:
            raise RuntimeError("Не удалось определить длительность видео")
        n_win = max(int(duration / window), 1)

        frames, ts = self.sample_video(path, sample_fps,
                                       progress=lambda p: progress and progress(p * 0.55))
        audio, sr = self.sample_audio(path,
                                      progress=lambda p: progress and progress(0.55 + p * 0.45))

        motion = np.zeros(n_win)
        bright = np.zeros(n_win)
        if len(frames) >= 2:
            d = np.abs(frames[1:].astype(np.int16) - frames[:-1].astype(np.int16))
            md = d.mean(axis=(1, 2))
            secs = np.clip(((ts[1:] + ts[:-1]) / 2 / window).astype(int), 0, n_win - 1)
            sm = np.bincount(secs, weights=md, minlength=n_win)
            cn = np.bincount(secs, minlength=n_win)
            motion = np.divide(sm, cn, out=np.zeros(n_win), where=cn > 0)
            bsec = np.clip((ts[1:] / window).astype(int), 0, n_win - 1)
            bm = frames[1:].astype(np.float64).mean(axis=(1, 2))
            bs = np.bincount(bsec, weights=bm, minlength=n_win)
            bc = np.bincount(bsec, minlength=n_win)
            bright = np.divide(bs, bc, out=np.zeros(n_win), where=bc > 0)

        rms = np.zeros(n_win)
        speech = np.zeros(n_win)
        pitch_var = np.zeros(n_win)
        win_samples = int(sr * window)
        if len(audio) > win_samples * 2 and win_samples > 16:
            usable = (len(audio) // win_samples) * win_samples
            W = audio[:usable].reshape(-1, win_samples)
            rms = np.sqrt((W ** 2).mean(axis=1))
            spec = np.abs(np.fft.rfft(W * np.hanning(win_samples)[None, :], axis=1))
            freqs = np.fft.rfftfreq(win_samples, 1 / sr)
            band = (freqs >= 80) & (freqs <= 3500)
            tot = spec.sum(axis=1) + 1e-9
            speech = spec[:, band].sum(axis=1) / tot
            voice_e = spec[:, band] + 1e-9
            cent = (voice_e * freqs[band]).sum(axis=1) / voice_e.sum(axis=1)
            pv = np.zeros(len(cent))
            pv[1:] = np.abs(np.diff(cent)) / (cent[1:] + cent[:-1] + 1e-9)
            pitch_var = pv
            if len(rms) < n_win:
                rms = np.pad(rms, (0, n_win - len(rms)))
                speech = np.pad(speech, (0, n_win - len(speech)))
                pitch_var = np.pad(pitch_var, (0, n_win - len(pitch_var)))
            else:
                rms, speech, pitch_var = rms[:n_win], speech[:n_win], pitch_var[:n_win]

        m_n = _normalize(motion)
        br_n = _normalize(bright)
        r_n = _normalize(rms)
        sp_n = _normalize(speech)
        pv_n = _normalize(pitch_var)

        # cut — локальный выброс движения над скользящей медианой
        med = np.array([np.median(motion[max(0, i - 5):i + 6]) for i in range(n_win)])
        cut = np.clip((m_n - med - 0.15) * 4.0, 0.0, 1.0)

        # tempo — разброс движения вокруг окна
        k = max(2, int(2.0 / window))
        tempo = np.array([m_n[max(0, i - k):i + k + 1].std() for i in range(n_win)])
        tempo = _normalize(tempo)

        times = (np.arange(n_win) + 0.5) * window

        return {
            "window": window,
            "duration": duration,
            "times": times.tolist(),
            "motion": m_n.tolist(),
            "brightness": br_n.tolist(),
            "cut": cut.tolist(),
            "rms": r_n.tolist(),
            "speech": sp_n.tolist(),
            "pitch_var": pv_n.tolist(),
            "tempo": tempo.tolist(),
        }
