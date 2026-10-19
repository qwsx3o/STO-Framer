import os
import sys
import time
import subprocess
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from PySide6.QtCore import QThread, Signal
from core.engine import VideoEngine
from core.logger import log, log_exception
from core.settings import APP_DIR, CACHE_DIR
from core.subtitles import Transcriber, extract_audio, chunk_words, write_ass


class RenderWorker(QThread):
    progress_changed = Signal(int, int, float)  # part, total, percentage
    status_changed = Signal(str)
    finished_all = Signal()
    failed = Signal(str)

    def __init__(self, input_file, out_dir, seg_minutes, blur, hflip, text_tpl, y_pos, plugins=None,
                 subs_enabled=False, subs_family="Arial", subs_size=72, subs_x=0.5, subs_y=0.75,
                 whisper_model="small", language="auto"):
        super().__init__()
        self.input_file = input_file
        self.out_dir = out_dir
        self.seg_len = max(1, seg_minutes * 60)
        self.blur = blur
        self.hflip = hflip
        self.text_tpl = text_tpl
        self.y_pos = y_pos
        self.plugins = plugins or []
        self.subs_enabled = subs_enabled
        self.subs_family = subs_family or "Arial"
        self.subs_size = subs_size
        self.subs_x = subs_x
        self.subs_y = subs_y
        self.whisper_model = whisper_model
        self.language = language
        self._is_cancelled = False
        self._process = None

    def cancel(self):
        """Останавливает цикл и немедленно прерывает текущий процесс FFmpeg."""
        self._is_cancelled = True
        if self._process and self._process.poll() is None:
            try:
                self._process.kill()
            except OSError:
                pass

    def run(self):
        current_out_path = None
        executor = None
        try:
            os.makedirs(self.out_dir, exist_ok=True)
            os.makedirs(CACHE_DIR, exist_ok=True)

            info = VideoEngine.inspect_file(self.input_file)
            total_dur = info.get("duration", 0)
            if total_dur <= 0:
                raise ValueError("Не удалось определить длительность входного файла или файл пуст.")

            parts = int(total_dur // self.seg_len) + (1 if total_dur % self.seg_len > 0 else 0)
            log(f"Старт: {os.path.basename(self.input_file)}, {total_dur:.0f} с, частей: {parts}, "
                f"субтитры: {'да' if self.subs_enabled else 'нет'}")

            # Предотвращает появление мигающих окон консоли на Windows
            creationflags = 0
            if sys.platform == "win32":
                creationflags = subprocess.CREATE_NO_WINDOW

            transcriber = None
            if self.subs_enabled:
                self.status_changed.emit("Загрузка модели Whisper (при первом запуске скачивается)...")
                transcriber = Transcriber(self.whisper_model)

            def prepare(idx):
                """Распознавание речи для части idx -> относительный путь к .ass (в фоновом потоке)."""
                if self._is_cancelled:
                    return None
                num = idx + 1
                start = idx * self.seg_len
                length = min(self.seg_len, total_dur - start)
                wav = os.path.join(CACHE_DIR, f"seg_audio_{num:02d}.wav")
                ass_path = os.path.join(CACHE_DIR, f"sub_{num:02d}.ass")
                t0 = time.time()

                def on_progress(pct, text):
                    self.status_changed.emit(f"Распознавание части {num}: {pct:.0f}%")
                    log(f"Часть {num}: {pct:3.0f}%  «{text[:60]}»")

                try:
                    log(f"Часть {num}: извлечение аудио...")
                    if not extract_audio(self.input_file, start, length, wav):
                        log(f"Часть {num}: не удалось извлечь аудио")
                        return None
                    log(f"Часть {num}: распознавание речи...")
                    words = transcriber.transcribe(wav, self.language, on_progress)
                    chunks = chunk_words(words)
                    log(f"Часть {num}: слов {len(words)}, фраз {len(chunks)}, "
                        f"распознано за {time.time() - t0:.0f} с")
                    if not chunks:
                        return None
                    write_ass(chunks, ass_path, self.subs_family, self.subs_size,
                              self.subs_x, self.subs_y)
                    return f"cache/sub_{num:02d}.ass"
                except Exception:
                    log_exception(f"Часть {num}: ошибка распознавания")
                    raise
                finally:
                    self._cleanup_file(wav)

            future = None
            if transcriber:
                executor = ThreadPoolExecutor(max_workers=1)
                future = executor.submit(prepare, 0)

            for i in range(parts):
                if self._is_cancelled:
                    break

                start_sec = i * self.seg_len
                dur = min(self.seg_len, total_dur - start_sec)
                part_num = i + 1

                self.progress_changed.emit(part_num, parts, (i / parts) * 100.0)

                ass_abs = os.path.join(CACHE_DIR, f"sub_{part_num:02d}.ass")
                ass_rel = None

                if future is not None:
                    self.status_changed.emit(f"Распознавание речи: часть {part_num} из {parts}...")
                    while not self._is_cancelled:
                        try:
                            ass_rel = future.result(timeout=0.5)
                            break
                        except FutureTimeout:
                            continue
                    if self._is_cancelled:
                        self._cleanup_file(ass_abs)
                        break
                    future = executor.submit(prepare, i + 1) if i + 1 < parts else None

                self.status_changed.emit(f"Рендеринг фрагмента {part_num} из {parts}...")
                log(f"Часть {part_num}: рендеринг FFmpeg ({'с субтитрами' if ass_rel else 'без субтитров'})...")
                t_render = time.time()

                out_filename = f"part_{part_num:02d}.mp4"
                current_out_path = os.path.join(self.out_dir, out_filename)

                filter_graph = VideoEngine.build_filter(
                    blur=self.blur,
                    hflip=self.hflip,
                    text_template=self.text_tpl,
                    part=part_num,
                    total=parts,
                    y_pos=self.y_pos,
                    ass_file=ass_rel
                )

                # Прогоняем через установленные плагины
                for p in self.plugins:
                    if getattr(p, "enabled", True) and hasattr(p, "modify_filter_graph"):
                        filter_graph = p.modify_filter_graph(filter_graph, part_num, parts)

                cmd = [
                    "ffmpeg", "-y",
                    "-ss", str(start_sec),
                    "-i", self.input_file,
                    "-t", str(dur),
                    "-filter_complex", filter_graph,
                    "-c:v", "libx264",
                    "-pix_fmt", "yuv420p",
                    "-preset", "faster",
                    "-crf", "19",
                    "-c:a", "aac",
                    "-b:a", "192k",
                    current_out_path
                ]

                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=creationflags,
                    cwd=APP_DIR
                )

                _, stderr_output = self._process.communicate()
                self._cleanup_file(ass_abs)

                if self._is_cancelled:
                    self._cleanup_file(current_out_path)
                    break

                if self._process.returncode != 0:
                    self._cleanup_file(current_out_path)
                    log("FFmpeg stderr:\n" + "\n".join(stderr_output.strip().splitlines()[-20:]))
                    err_tail = "\n".join(stderr_output.strip().splitlines()[-5:])
                    raise RuntimeError(f"FFmpeg завершился с ошибкой на части {part_num}:\n{err_tail}")

                log(f"Часть {part_num}: готово за {time.time() - t_render:.0f} с -> {out_filename}")

            if self._is_cancelled:
                self.status_changed.emit("Рендеринг отменен.")
            else:
                self.progress_changed.emit(parts, parts, 100.0)
                self.status_changed.emit("Рендеринг завершен.")
                self.finished_all.emit()

        except Exception as e:
            log_exception("Критическая ошибка конвейера")
            self._cleanup_file(current_out_path)
            self.failed.emit(str(e))
        finally:
            self._process = None
            if executor:
                executor.shutdown(wait=False, cancel_futures=True)

    def _cleanup_file(self, path):
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass