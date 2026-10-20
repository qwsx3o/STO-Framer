import os
import re
import shutil
import subprocess

from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QFileDialog
from PySide6.QtCore import QThread, Signal
from qfluentwidgets import (
    LineEdit, ComboBox, PushButton, PrimaryPushButton, BodyLabel,
    CheckBox, ProgressBar, InfoBar, InfoBarPosition,
)

from core.plugin_base import BasePlugin
from core.settings import CACHE_DIR
from core.logger import log, log_exception

# Форматы: подпись -> форматная строка yt-dlp.
# Раздельные видео+аудио дорожки склеиваются встроенным в приложение FFmpeg в mp4.
QUALITY_FORMATS = {
    "Максимальное доступное (до 4K)": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
    "1080p (Full HD)": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]",
    "720p (HD)": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[height<=720]",
    "480p": "bestvideo[height<=480]+bestaudio[ext=m4a]/best[height<=480]/best",
}

_SAFE_NAME = re.compile(r"[^\w\-. ]+", re.UNICODE)

# Кэш результата проверки DNS (сбрасывается при каждой загрузке)
_dns_probe = {}


def _doh_raw(server_ip: str, names) -> dict:
    """DNS-over-HTTPS запрос напрямую к IP резолвера (минуя системный DNS целиком).

    server_ip: "8.8.8.8" (Google DoH) или "1.1.1.1" (Cloudflare).
    Возвращает {host: [ipv4,...]}; пустой dict — если сервер недоступен.
    Имя хоста в SNI/сертификате не проверяется (IP вместо имени), но данные DNS
    подписаны DNSSEC на стороне резолвера; используем два независимых источника.
    """
    import json as _json
    import socket
    import ssl
    import http.client

    out = {}
    try:
        ctx = ssl.create_default_context()
        # соединяемся по IP, но сертификат Google/Cloudflare выдан на их домен
        conn = http.client.HTTPSConnection(server_ip, 443, timeout=6, context=ctx)
        conn.sock = socket.create_connection((server_ip, 443), timeout=6)
        conn.sock = ctx.wrap_socket(conn.sock, server_hostname=(
            "dns.google" if server_ip == "8.8.8.8" else "cloudflare-dns.com"))
        for name in names:
            path = (f"/resolve?name={name}&type=A" if server_ip == "8.8.8.8"
                    else f"/dns-query?name={name}&type=A")
            headers = {"Host": "dns.google" if server_ip == "8.8.8.8" else "cloudflare-dns.com",
                       "Accept": "application/dns-json", "User-Agent": "Mozilla/5.0"}
            try:
                conn.request("GET", path, headers=headers)
                resp = conn.getresponse()
                data = _json.loads(resp.read().decode())
                ips = [a["data"] for a in data.get("Answer", []) if a.get("type") == 1]
                if ips:
                    out[name] = ips
            except Exception:
                continue
        try:
            conn.close()
        except Exception:
            pass
    except Exception:
        return {}
    return out


def resolve_yt_hosts(hosts=None) -> list:
    """Резолвит хосты YouTube через все доступные источники.

    Порядок: системный DNS (обычно работает — тогда DoH не нужен) → DoH через urllib
    (если dns.google резолвится штатно) → DoH «в лоб» к IP 8.8.8.8/1.1.1.1 через raw TLS
    (единственный путь, когда резолвер Windows мёртв полностью, ошибка 11001).
    Возвращает список уникальных IPv4 для hosts (по умолчанию — www.youtube.com);
    пустой список — если ни один источник не ответил.
    """
    import socket
    hosts = list(hosts or ["www.youtube.com"])
    ips = []

    def _add(addr):
        if addr and addr.count(".") == 3 and addr not in ips:
            ips.append(addr)

    # 1) Системный DNS (обычный путь)
    for h in hosts:
        try:
            for r in socket.getaddrinfo(h, 443, socket.AF_INET):
                _add(r[4][0])
        except OSError:
            pass
    if ips:
        return ips

    # 2) DoH через dns.google / cloudflare-dns.com обычным urllib-путём
    import json as _json
    import urllib.request

    def _doh(server):
        try:
            req = urllib.request.Request(
                f"https://dns.google/resolve?name={hosts[0]}&type=A",
                headers={"User-Agent": "Mozilla/5.0"}, method="GET") if "dns.google" in server else None
            if req is None:  # cloudflare-dns.com
                req = urllib.request.Request(
                    f"https://{server}/dns-query?name={hosts[0]}&type=A",
                    headers={"Accept": "application/dns-json", "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = _json.loads(resp.read().decode())
            return [a["data"] for a in data.get("Answer", []) if a.get("type") == 1]
        except Exception:
            return []

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as ex:
        for got in ex.map(_doh, ["https://dns.google/resolve", "cloudflare-dns.com"]):
            for ip in got:
                _add(ip)
    if ips:
        return ips

    # 3) ДоH по IP напрямую (8.8.8.8 и 1.1.1.1 параллельно) — обходит мёртвый резолвер
    results = {}
    def _raw(ip):
        return _doh_raw(ip, hosts)
    with ThreadPoolExecutor(max_workers=2) as ex:
        for got in ex.map(_raw, ["8.8.8.8", "1.1.1.1"]):
            for host, addrs in got.items():
                results.setdefault(host, []).extend(addrs)
    for host in hosts:
        for ip in dict.fromkeys(results.get(host, [])):
            _add(ip)
    return ips


# Хосты YouTube/Google, которые нужно резолвить в обход системы. ВАЖНО: googlevideo.com
# (CDN стримов) сюда НЕ входит — его резолвит система, а при битом DNS срабатывает
# connect-хук, который сам попросит DoH для любого хоста, не указанного в IP.
_YT_PIN_HOSTS = {"www.youtube.com", "youtube.com", "m.youtube.com", "music.youtube.com",
                 "www.youtube-nocookie.com", "youtubei.googleapis.com", "www.googleapis.com",
                 "scone-pa.clients6.google.com", "manifest.googlevideo.com",
                 "i.ytimg.com", "s.ytimg.com", "static.youtube.com"}

_DNS_BLOCK_ERRNO = 11001  # Windows WSANOTINITIALISED... нет: EAI_NONAME — «имя не найдено»


def _is_ip(s: str) -> bool:
    import socket
    try:
        socket.inet_aton(str(s))
        return True
    except OSError:
        return False


def _install_dns_shim():
    """Обход сломанного системного DNS для загрузок yt-dlp.

    На машинах с Errno 11001 (getaddrinfo failed) резолвер Windows не отвечает НИКОМУ,
    включая urllib, поэтому DoH-запросы к dns.google тоже падают. Решение — два уровня:

    1) getaddrinfo: для известных хостов YouTube отдаём IP, полученные через DoH
       (raw-TLS запрос к 8.8.8.8 минуется резолвер; если есть обычный интернет — берём
       системный резолв). Если IP ещё нет — регистрируем хост в очередь и возвращаем
       placeholder, чтобы соединение дошло до уровня connect.
    2) socket.connect: любой hostname-адрес (например *.googlevideo.com или dns.google)
       резолвится лениво тем же механизмом и подменяется на IP — так TLS/SNI продолжают
       работать с настоящим именем сервера (yt-dlp оборачивает сокет с server_hostname=host).

    Подмена глобальная, но живёт только на время загрузки (_uninstall_dns_shim в finally).
    """
    global _dns_probe
    if _dns_probe.get("installed"):
        return
    import socket

    state = {"ips": {}, "pending": set(), "orig_gai": socket.getaddrinfo,
             "orig_connect": socket.socket.connect, "resolving": False}
    _dns_probe = {"installed": True, "state": state}

    def _ensure_ips(host):
        """IP для хоста: из кэша, иначе резолв (системный → DoH). Пустой список при неудаче."""
        if host in state["ips"]:
            return state["ips"][host]
        if _is_ip(host):  # сам IP — резолв не нужен (иначе DoH-запросы к 8.8.8.8 зациклятся)
            return [host]
        ips = []
        try:
            for r in state["orig_gai"](host, 443, socket.AF_INET):
                if r[4][0] not in ips:
                    ips.append(r[4][0])
        except OSError:
            ips = []
        if not ips and not state["resolving"]:
            # resolving=True защищает от рекурсии: DoH-запросы внутри резолвят сами себя
            state["resolving"] = True
            try:
                for ip in resolve_yt_hosts([host]):
                    if ip not in ips:
                        ips.append(ip)
            finally:
                state["resolving"] = False
        if ips:
            state["ips"][host] = ips
        return ips

    def gai_patched(host, port, family=0, type=0, proto=0, flags=0):
        h = str(host)
        if not _is_ip(h):
            if h in _YT_PIN_HOSTS or state["resolving"]:
                ips = _ensure_ips(h)
                if ips:
                    return [(socket.AF_INET, socket.SOCK_STREAM, proto or 6, "", (ip, port))
                            for ip in ips]
                # IP пока недоступен: placeholder нужен, чтобы код дошёл до connect,
                # где хост будет зарезолвлен лениво (см. connect_patched)
                return [(socket.AF_INET, socket.SOCK_STREAM, proto or 6, "", ("192.0.2.1", port))]
            # Прочие имена (например googlevideo CDN) — пробуем системный резолвер;
            # если он мёртв, исключение перехватит connect-уровень ниже по стеку? Нет —
            # create_connection зовёт getaddrinfo ДО connect. Резолвим лениво и здесь.
            ips = _ensure_ips(h)
            if ips:
                return [(socket.AF_INET, socket.SOCK_STREAM, proto or 6, "", (ip, port))
                        for ip in ips]
        return state["orig_gai"](host, port, family, type, proto, flags)

    def connect_patched(self, addr, *a, **kw):
        orig = state["orig_connect"]
        try:
            if isinstance(addr, tuple) and len(addr) >= 2 and not _is_ip(addr[0]):
                host = str(addr[0])
                ips = _ensure_ips(host)
                if ips:
                    addr = (ips[0], addr[1])
            return orig(self, addr, *a, **kw)
        except OSError as e:
            # Задержка создания сокета / гонка: повторяем один раз после резолва
            if isinstance(addr, tuple) and not _is_ip(addr[0]):
                host = str(addr[0])
                ips = _ensure_ips(host)
                if ips:
                    return orig(self, (ips[0], addr[1]), *a, **kw)
            raise

    socket.getaddrinfo = gai_patched
    socket.socket.connect = connect_patched
    log("YouTube: включён обход системного DNS (DoH через прямой TLS к 8.8.8.8)")


def _uninstall_dns_shim():
    global _dns_probe
    st = _dns_probe.get("state")
    if _dns_probe.get("installed") and st:
        import socket
        try:
            if st.get("orig_gai"):
                socket.getaddrinfo = st["orig_gai"]
            if st.get("orig_connect"):
                socket.socket.connect = st["orig_connect"]
        except Exception:
            pass
    _dns_probe = {}


def _safe_filename(name: str) -> str:
    """Убирает символы, недопустимые в именах файлов на Windows/Linux."""
    name = _SAFE_NAME.sub("_", name).strip(" ._")
    return name[:150] if name else "video"


def chrome_cookie_db() -> str:
    """Путь к базе cookies Chrome (или None, если не найдена)."""
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = []
    if local:
        # Обычный профиль и профиль с обфусцированным именем (GameDB / enterprise-сборки)
        candidates.append(os.path.join(local, "Google", "Chrome", "User Data", "Default", "Cookies"))
        prof_root = os.path.join(local, "Google", "Chrome", "User Data")
        try:
            for entry in os.listdir(prof_root):
                p = os.path.join(prof_root, entry, "Default", "Cookies")
                if os.path.isfile(p) and p not in candidates:
                    candidates.append(p)
        except OSError:
            pass
    home = os.path.expanduser("~")
    candidates += [
        os.path.join(home, ".config", "google-chrome", "Default", "Cookies"),
        os.path.join(home, ".config", "chromium", "Default", "Cookies"),
        os.path.join(home, "Library", "Application Support", "Google", "Chrome", "Default", "Cookies"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def _chrome_master_key() -> bytes:
    """AES-ключ Chrome из Local State. Windows: DPAPI; Linux: keyring/файл; macOS: ключ в связке."""
    import base64, json
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [os.path.join(local, "Google", "Chrome", "User Data", "Local State")] if local else []
    home = os.path.expanduser("~")
    candidates += [
        os.path.join(home, ".config", "google-chrome", "Local State"),
        os.path.join(home, ".config", "chromium", "Local State"),
        os.path.join(home, "Library", "Application Support", "Google", "Chrome", "Local State"),
    ]
    enc_key_b64 = None
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as f:
                enc_key_b64 = json.load(f)["os_crypt"]["encrypted_key"]
            break
        except (OSError, KeyError, ValueError):
            continue
    if not enc_key_b64:
        raise RuntimeError("Не найден файл Local State с ключом шифрования Chrome")
    raw = base64.b64decode(enc_key_b64)[5:]  # отбрасываем префикс "DPAPI"
    if os.name == "nt":
        try:
            import win32crypt
        except ImportError:
            raise RuntimeError("Нужна библиотека pywin32: pip install pywin32")
        return win32crypt.CryptUnprotectData(raw, None, None, None, 0)[1]
    # Linux/macOS: browser_cookie3 умеет извлекать master key сам — сюда не доходим
    raise RuntimeError("Мастер-ключ вне Windows не поддерживается напрямую")


def _decrypt_chrome_values(rows: list) -> list:
    """Расшифровка encrypted_value для строк cookie-базы Chrome.

    rows: [(host_key, name, path, expires_utc, is_secure, encrypted_value), ...]
    Возвращает список байтов-значений (пустые там, где расшифровать не удалось).
    """
    results = [b"" for _ in rows]
    if os.name != "nt":
        # Linux/macOS: пусть это сделает browser_cookie3 (keychain/keyring обвязка)
        try:
            import browser_cookie3
            cj = browser_cookie3.chrome(domain_name="youtube.com")
        except Exception as e:
            raise RuntimeError(f"Не удалось прочитать cookies Chrome: {e}")
        lookup = {(c.domain, c.name): (getattr(c, "value", "") or "").encode("utf-8", "replace")
                  for c in cj}
        for i, row in enumerate(rows):
            results[i] = lookup.get((row[0], row[1]), b"")
        if not any(results):
            raise RuntimeError("Chrome вернул пустой набор cookies (закрыт профиль или нет авторизации на YouTube)")
        return results

    try:
        from Crypto.Cipher import AES
    except ImportError:
        raise RuntimeError("Нужна библиотека pycryptodome: pip install pycryptodome")
    key = _chrome_master_key()
    for i, row in enumerate(rows):
        blob = row[5] or b""
        try:
            if blob[:3] in (b"v10", b"v11", b"v20"):  # AES-GCM (Chrome 80+ / 127+)
                nonce, payload = blob[3:15], blob[15:]
                ct, tag = payload[:-16], payload[-16:]
                results[i] = AES.new(key, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(ct, tag)
            elif os.name == "nt":  # старый DPAPI без префикса
                import win32crypt
                results[i] = win32crypt.CryptUnprotectData(blob, None, None, None, 0)[1]
        except Exception:
            results[i] = b""  # пропускаем нерасшифрованные (например, сервисные)
    if not any(results):
        raise RuntimeError("Не удалось расшифровать cookies Chrome (попробуйте экспорт через расширение Get Cookies.txt)")
    return results


def export_chrome_cookies(dest_path: str) -> int:
    """Экспортирует cookies YouTube из базы Chrome в файл формата Netscape.

    Читает SQLite напрямую (через копию файла — чтобы не мешал запущенный Chrome),
    расшифровывает значения (Windows: master key + AES-GCM; Linux/macOS: browser_cookie3).
    Возвращает количество сохранённых cookies; бросает RuntimeError с
    человекочитаемым текстом, если что-то из шагов недоступно.
    """
    import sqlite3
    db = chrome_cookie_db()
    if not db:
        raise RuntimeError("База cookies Chrome не найдена (убедитесь, что Chrome установлен)")

    tmp = dest_path + ".tmpdb"
    shutil.copy2(db, tmp)
    try:
        con = sqlite3.connect(tmp)
        try:
            rows = con.execute(
                "SELECT host_key, name, path, expires_utc, is_secure, encrypted_value "
                "FROM cookies WHERE host_key LIKE '%youtube.com%'").fetchall()
        finally:
            con.close()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    if not rows:
        raise RuntimeError("В Chrome нет cookies youtube.com — откройте youtube.com и войдите в аккаунт")

    values = _decrypt_chrome_values(rows)

    with open(dest_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("# Netscape HTTP Cookie File\n# exported by STO Framer Studio (YouTube module)\n\n")
        for (host, name, path, exp, secure, _enc), val in zip(rows, values):
            if not val:
                continue
            expiry = 0
            if exp:  # WebKit time: мкс с 1601-01-01
                try:
                    expiry = max(0, int(exp / 1_000_000 - 11_644_473_600))
                except (TypeError, ValueError):
                    expiry = 0
            # В Netscape-формате flag include_subdomains обязан соответствовать
            # ведущей точке домена, иначе парсер (http.cookiejar у yt-dlp) падает.
            initial_dot = host.startswith(".")
            f.write(f"{host}\t{'TRUE' if initial_dot else 'FALSE'}\t{path or '/'}\t{'TRUE' if secure else 'FALSE'}"
                    f"\t{expiry}\t{name}\t{val.decode('utf-8', 'replace')}\n")
    return sum(1 for v in values if v)


class CookieExportWorker(QThread):
    """Экспорт cookies из Chrome вне потока GUI (чтение БД + расшифровка)."""
    done = Signal(bool, str)  # успех, текст/путь

    def run(self):
        try:
            dest = os.path.join(CACHE_DIR, "yt_cookies.txt")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            n = export_chrome_cookies(dest)
            self.done.emit(True, f"{dest}|{n}")
        except Exception as e:
            log_exception(f"YouTube: экспорт cookies из Chrome не удался: {e}")
            self.done.emit(False, str(e))


class DownloadWorker(QThread):
    """Скачивание через yt-dlp в фоновом потоке, чтобы не блокировать UI."""
    progress_changed = Signal(int)   # процент 0..100
    status_changed = Signal(str)     # человекочитаемый статус
    finished_ok = Signal(str)        # путь к скачанному файлу
    failed = Signal(str)

    def __init__(self, url: str, fmt: str, cookies_file: str = "",
                 use_browser_cookies: bool = False, out_dir: str = None):
        super().__init__()
        self.url = url.strip()
        self.fmt = fmt
        self.cookies_file = cookies_file
        self.use_browser_cookies = use_browser_cookies
        self.out_dir = out_dir or os.path.join(CACHE_DIR, "downloads")
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def _hook(self, d):
        import yt_dlp
        if self._cancelled:
            raise yt_dlp.utils.DownloadCancelled("Отменено пользователем")

        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes", 0)
            if total:
                self.progress_changed.emit(int(done * 100 / total))
            speed = d.get("speed") or 0
            eta = d.get("eta")
            self.status_changed.emit(
                f"Загрузка... {done / 1048576:.1f} МБ"
                + (f" из {total / 1048576:.1f} МБ" if total else "")
                + (f" · {speed / 1048576:.1f} МБ/с" if speed else "")
                + (f" · осталось {eta} с" if eta else "")
            )
        elif d["status"] == "finished":
            self.status_changed.emit("Склейка видео- и аудиодорожки (FFmpeg)...")
            self.progress_changed.emit(99)

    def run(self):
        final_path = None
        try:
            import yt_dlp  # ленивый импорт: без зависимости приложение не падает

            os.makedirs(self.out_dir, exist_ok=True)

            opts = {
                "format": self.fmt,
                "outtmpl": os.path.join(self.out_dir, "%(title)s.%(ext)s"),
                "progress_hooks": [self._hook],
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                # merge_output_format требует ffmpeg в PATH — он уже нужен приложению
                "merge_output_format": "mp4",
            }
            if self.cookies_file and os.path.isfile(self.cookies_file):
                opts["cookiefile"] = self.cookies_file
                log(f"YouTube: используются cookies из {self.cookies_file}")
            if self.use_browser_cookies:
                # Чтение cookies из браузера может требовать подтверждения на Windows
                opts["cookiesfrombrowser"] = ("chrome",)

            self.status_changed.emit("Получение информации о видео...")
            _uninstall_dns_shim()  # на случай остаточного состояния после прошлой загрузки
            try:
                # Обход включаем СРАЗУ, а не после первой ошибки: если системный DNS
                # сломан (Errno 11001 getaddrinfo failed), первая попытка без шима
                # обречена, а DoH-резолв внутри шима работает даже при мёртвом резолвере.
                # Если DNS в порядке — шим прозрачно использует его как обычно.
                _install_dns_shim()
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(self.url, download=True)
            except yt_dlp.utils.DownloadError as e:
                if not self._is_network_error(e):
                    raise
                log("YouTube: сетевая ошибка с активным обходом — повторяю попытку...")
                self.status_changed.emit("Сетевая ошибка — повторная попытка через DNS-over-HTTPS...")
                _install_dns_shim()
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(self.url, download=True)
            finally:
                _uninstall_dns_shim()
            title = _safe_filename(info.get("title", "video"))
            for ext in ("mp4", "mkv", "webm"):
                candidate = os.path.join(self.out_dir, f"{title}.{ext}")
                if os.path.exists(candidate):
                    final_path = candidate
                    break
            if not final_path:
                requested = info.get("requested_downloads") or []
                if requested:
                    final_path = requested[0].get("filepath")

            if final_path and os.path.exists(final_path):
                size_mb = os.path.getsize(final_path) / 1048576
                log(f"YouTube: готово — {os.path.basename(final_path)} ({size_mb:.1f} МБ)")
                self.progress_changed.emit(100)
                self.finished_ok.emit(final_path)
            else:
                self.failed.emit("Файл не найден после загрузки (неизвестная ошибка yt-dlp).")

        except Exception as e:
            _uninstall_dns_shim()
            log_exception("YouTube: ошибка скачивания")
            msg = self._friendly_error(e)
            if final_path and os.path.exists(final_path):
                # Скачали, но склейка упала — отдаём то, что есть
                self.finished_ok.emit(final_path)
            else:
                self.failed.emit(msg)

    @staticmethod
    def _is_network_error(e) -> bool:
        s = str(e).lower()
        return ("getaddrinfo" in s or "gaierror" in s or "temporary failure in name resolution" in s
                or "name or service not known" in s or "network is unreachable" in s)

    @classmethod
    def _friendly_error(cls, e) -> str:
        """Одно предложение вместо простыни трейсбека yt-dlp."""
        raw = str(e)
        low = raw.lower()
        # Вытаскиваем главное сообщение ExtractorError/DownloadError, если оно есть
        m = re.search(r"ERROR:\s*(.+?)(?:\n|$)", raw)
        core = (m.group(1).strip() if m else raw.splitlines()[0])[:300]
        if cls._is_network_error(e):
            return ("Не удалось подключиться к YouTube: ошибка DNS (getaddrinfo failed), "
                    "несмотря на автоматическую повторную попытку через DNS-over-HTTPS. "
                    "Обычно это блокировка провайдера/антивируса или проблемы с интернетом. "
                    "Что попробовать: 1) выключить VPN/прокси либо наоборот включить; "
                    "2) временно отключить антивирус/файрвол; 3) проверить hosts-файл "
                    "C:\\Windows\\System32\\drivers\\etc\\hosts (строки с youtube.com нужно удалить); "
                    "4) сменить DNS адаптера на 8.8.8.8. ")
        if "not a bot" in low:
            return core + (" | Решение: нажмите «Получить cookies из Chrome автоматически» "
                           "в модуле и повторите скачивание.")
        return core


class YouTubeDownloadPlugin(BasePlugin):
    """Модуль загрузки исходников с YouTube в максимальном качестве."""
    name = "Загрузка с YouTube"
    description = ("Скачивает видео с YouTube напрямую с серверов Google в максимальном "
                   "доступном качестве (yt-dlp) вместо посредников низкого качества. "
                   "Файл сохраняется в cache/downloads и подставляется как источник нарезки.")
    version = "1.0.0"

    def __init__(self):
        super().__init__()
        self.is_active = False
        self.quality = next(iter(QUALITY_FORMATS))
        self.cookies_file = ""
        self._worker = None
        # Колбэк приложения: callable(path) -> None, вызывается после успешной загрузки
        self.on_video_ready = None

    # ------------------------------------------------------------------ UI
    def create_ui(self, parent: QWidget) -> QWidget:
        container = QWidget(parent)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(8)

        self.chk_enable = CheckBox("Включить модуль загрузки с YouTube", container)
        self.chk_enable.setChecked(self.is_active)
        self.chk_enable.stateChanged.connect(self._on_toggle)
        layout.addWidget(self.chk_enable)

        self.ed_url = LineEdit(container)
        self.ed_url.setPlaceholderText("Вставьте ссылку на видео YouTube...")
        self.ed_url.setEnabled(False)
        layout.addWidget(self.ed_url)

        q_box = QHBoxLayout()
        q_box.addWidget(BodyLabel("Качество:", container))
        self.combo_quality = ComboBox(container)
        self.combo_quality.addItems(list(QUALITY_FORMATS.keys()))
        self.combo_quality.currentIndexChanged.connect(
            lambda i: setattr(self, "quality", list(QUALITY_FORMATS)[i])
        )
        self.combo_quality.setEnabled(False)
        q_box.addWidget(self.combo_quality, 1)
        layout.addLayout(q_box)

        self.chk_browser_cookies = CheckBox("Cookies из браузера Chrome (обход антибот-проверки)", container)
        self.chk_browser_cookies.setEnabled(False)
        layout.addWidget(self.chk_browser_cookies)

        ck_box = QHBoxLayout()
        self.btn_cookies = PushButton("Файл cookies.txt", container)
        self.btn_cookies.setEnabled(False)
        self.btn_cookies.clicked.connect(self._pick_cookies)
        self.lbl_cookies = BodyLabel("не задан", container)
        self.lbl_cookies.setWordWrap(True)
        ck_box.addWidget(self.btn_cookies)
        ck_box.addWidget(self.lbl_cookies, 1)
        layout.addLayout(ck_box)

        # Автоэкспорт cookies прямо из базы Chrome — пользователю не нужно ничего ставить руками
        self.btn_auto_cookies = PushButton("Получить cookies из Chrome автоматически", container)
        self.btn_auto_cookies.setEnabled(False)
        self.btn_auto_cookies.clicked.connect(self._auto_cookies)
        layout.addWidget(self.btn_auto_cookies)

        btn_box = QHBoxLayout()
        self.btn_download = PrimaryPushButton("Скачать видео", container)
        self.btn_download.setEnabled(False)
        self.btn_download.clicked.connect(self._start_download)
        self.btn_cancel = PushButton("Отмена", container)
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._cancel_download)
        btn_box.addWidget(self.btn_download, 1)
        btn_box.addWidget(self.btn_cancel)
        layout.addLayout(btn_box)

        self.progress = ProgressBar(container)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.lbl_status = BodyLabel("", container)
        self.lbl_status.setWordWrap(True)
        layout.addWidget(self.lbl_status)

        return container

    # ------------------------------------------------------------------ handlers
    def _on_toggle(self, state):
        self.is_active = bool(state)
        self._set_inputs_enabled(self._worker is None)
        if self.is_active and not shutil.which("ffmpeg"):
            self.lbl_status.setText("Внимание: FFmpeg не найден в PATH — склейка раздельных потоков будет недоступна.")

    def _set_inputs_enabled(self, free: bool):
        on = self.is_active and free
        for w in (self.ed_url, self.combo_quality, self.chk_browser_cookies,
                  self.btn_cookies, self.btn_auto_cookies):
            w.setEnabled(on)
        self.btn_download.setEnabled(on)
        self.btn_cancel.setEnabled(not free and self.is_active)

    def _auto_cookies(self):
        """Фоновый экспорт cookies YouTube из базы Chrome + автотест загрузки."""
        if getattr(self, "_ck_worker", None) and self._ck_worker.isRunning():
            return
        self.btn_auto_cookies.setEnabled(False)
        self.lbl_status.setText("Экспорт cookies из Chrome...")
        self._ck_worker = CookieExportWorker()
        self._ck_worker.done.connect(self._on_cookies_done)
        self._ck_worker.start()

    def _on_cookies_done(self, ok: bool, payload: str):
        self.btn_auto_cookies.setEnabled(self.is_active)
        if ok:
            path, n = payload.rsplit("|", 1)
            self.cookies_file = path
            self.lbl_cookies.setText(f"{os.path.basename(path)} ({n} шт., автоэкспорт)")
            self.lbl_status.setText(
                f"Cookies получены автоматически ({n} шт.). Попробуйте скачать видео ещё раз.")
            InfoBar.success("Готово", f"Экспортировано cookies из Chrome: {n}",
                            parent=self.ed_url, position=InfoBarPosition.TOP)
        else:
            self.lbl_status.setText(f"Автоэкспорт не удался: {payload}")
            InfoBar.warning(
                "Автоэкспорт недоступен",
                "Скопируйте cookies вручную: откройте youtube.com в браузере и "
                "используйте расширение «Get cookies.txt LOCALLY» → Сохранить → файл cookies.txt",
                parent=self.ed_url, position=InfoBarPosition.TOP, duration=15000)

    def _pick_cookies(self):
        path, _ = QFileDialog.getOpenFileName(
            self.ed_url, "Выберите экспорт cookies.txt (формат Netscape)", "",
            "Cookies (*.txt);;Все файлы (*)"
        )
        if path:
            self.cookies_file = path
            self.lbl_cookies.setText(os.path.basename(path))

    def _start_download(self):
        url = self.ed_url.text().strip()
        if not re.match(r"^https?://", url):
            InfoBar.warning("Внимание", "Введите корректную ссылку (https://...)",
                            parent=self.ed_url, position=InfoBarPosition.TOP)
            return

        try:
            import yt_dlp  # noqa: F401 — проверяем зависимость до запуска потока
        except ImportError:
            InfoBar.error("Ошибка", "Не установлен пакет yt-dlp.\nВыполните: pip install -U yt-dlp",
                          parent=self.ed_url, position=InfoBarPosition.TOP, duration=-1)
            return

        self._worker = DownloadWorker(
            url=url,
            fmt=QUALITY_FORMATS[self.combo_quality.currentText()],
            cookies_file=self.cookies_file,
            use_browser_cookies=self.chk_browser_cookies.isChecked(),
        )
        self._worker.status_changed.connect(lambda t: self.lbl_status.setText(t))
        self._worker.progress_changed.connect(self.progress.setValue)
        self._worker.finished_ok.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)

        log(f"YouTube: старт загрузки {url} [{self.combo_quality.currentText()}]")
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self._set_inputs_enabled(False)
        self._worker.start()

    def _cancel_download(self):
        if self._worker:
            self._worker.cancel()
            self.lbl_status.setText("Отмена...")

    def _on_done(self, path):
        self._cleanup_worker()
        self.progress.setVisible(False)
        InfoBar.success("Готово", f"Видео скачано: {os.path.basename(path)}",
                        parent=self.ed_url, position=InfoBarPosition.TOP)
        self.lbl_status.setText(f"Сохранено: {path}")
        if callable(self.on_video_ready):
            self.on_video_ready(path)

    def _on_failed(self, err):
        self._cleanup_worker()
        self.progress.setVisible(False)
        # Полная (неразмытая) версия ошибки — в статус-строке под полем ввода
        self.lbl_status.setText(f"Ошибка: {err}")
        # Человекочитаемое объяснение вместо сырого текста yt-dlp
        low = str(err).lower()
        if "not a bot" in low:
            hint = ("YouTube требует подтверждения, что вы не бот. Нажмите «Получить cookies из Chrome "
                    "автоматически» ниже (или в окне ошибки) и повторите скачивание.")
        elif "video is private" in low or "sign in" in low and "bot" not in low:
            hint = "Видео приватное или age-restricted — нужны cookies аккаунта, который имеет к нему доступ."
        elif "unsupported url" in low:
            hint = "Ссылка не распознана — проверьте, что это видео/плелист YouTube."
        elif "cookie" in low and ("decrypt" in low or "safe storage" in low):
            hint = "Не удалось прочитать cookies браузера — используйте автоэкспорт или файл cookies.txt."
        else:
            hint = ""
        short = " ".join(str(err).split())
        if len(short) > 200:
            short = short[:200] + "…"
        first = short.split(". ")[0].rstrip(".")
        if len(first) >= 40:
            first = first[:40].rstrip() + "…"
        bar = InfoBar.error(
            title="Скачивание не удалось",
            content=(hint if hint else short),
            parent=self.ed_url,
            position=InfoBarPosition.TOP,
            duration=-1 if hint else 15000,
        )
        # Если заголовок влезает целиком и отличается от тела — показываем его полностью
        try:
            if first and first != short:
                bar.title = first
                bar.titleLabel.setText(first)
        except Exception:
            pass
        # Кнопка с полным текстом ошибки (всплывающая подсказка обрезается, поэтому кнопка)
        try:
            from qfluentwidgets import MessageBox
            box = MessageBox("Полный текст ошибки", str(err), self.ed_url.window())
            bar.addActionButton(PushButton("Подробности", bar), lambda: box.exec())
        except Exception:
            pass
        # Если это антибот-блокировка — предлагаем автоматическое получение cookies из Chrome
        if "not a bot" in str(err).lower():
            def _autofix():
                box.close()
                self._auto_cookies()
            try:
                bar.addActionButton(PrimaryPushButton("Cookies из Chrome", bar), _autofix)
            except Exception:
                pass

    def _cleanup_worker(self):
        if self._worker:
            self._worker.wait(3000)
            self._worker = None
        self._set_inputs_enabled(True)

    # ------------------------------------------------------------------ hooks
    def modify_filter_graph(self, current_filter: str, segment_index: int, total_segments: int) -> str:
        # Модуль работает на этапе подготовки источника, фильтры кадра не меняет.
        return current_filter
