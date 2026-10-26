"""串流影音下載（HLS / DASH / 影音網站），以 yt-dlp 為核心。"""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Callable

from ..config import Settings
from ..models import Status, Task
from ..utils import human_size, sanitize_filename, write_netscape_cookies
from .http_engine import DEFAULT_UA, DownloadError
from .tools import deno_path, ensure_executable, ffmpeg_path

AUDIO_MP3 = "audio:mp3"
AUDIO_BEST = "audio:best"
MANIFEST_EXTS = (".m3u8", ".mpd")
# 一定是一般檔案的副檔名：影音網站的圖床（例如 i.imgur.com/xxx.png）也會被 yt-dlp 的解析器認領，
# 但 yt-dlp 只處理影片，靜態圖片會失敗（「… is not a video or animated image」），要直接下載
PLAIN_FILE_EXTS = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif", ".heic", ".heif", ".jxl", ".svg", ".tif", ".tiff",
    ".ico", ".pdf", ".txt", ".csv", ".json", ".xml", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".epub",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".zst", ".iso", ".dmg", ".exe", ".msi", ".apk",
    ".deb", ".rpm", ".appimage",
)


class _Logger:
    def __init__(self):
        self.last_error = ""

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        self.last_error = str(msg)


@lru_cache(maxsize=1)
def _site_extractors():
    from yt_dlp.extractor import gen_extractor_classes

    return [ie for ie in gen_extractor_classes() if ie.ie_key() != "Generic"]


def is_media_site(url: str) -> bool:
    """網址是否屬於 yt-dlp 有專屬解析器的影音網站（YouTube、Bilibili…）。圖片、文件等一般檔案除外。"""
    path = url.split("?", 1)[0].split("#", 1)[0].lower()
    if path.endswith(PLAIN_FILE_EXTS):
        return False
    try:
        return any(ie.suitable(url) for ie in _site_extractors())
    except Exception:  # noqa: BLE001
        return False


def is_manifest_url(url: str) -> bool:
    path = url.split("?", 1)[0].split("#", 1)[0].lower()
    return path.endswith(MANIFEST_EXTS)


def _base_opts(task: Task, settings: Settings, workdir: Path) -> dict:
    headers = {"User-Agent": task.user_agent or DEFAULT_UA}
    if task.referer:
        headers["Referer"] = task.referer
    for k, v in (task.headers or {}).items():
        if k.lower() not in ("cookie", "range", "host", "content-length", "accept-encoding"):
            headers[k] = v
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "http_headers": headers,
        "socket_timeout": 30,
        "retries": 10,
        "fragment_retries": 10,
        "windowsfilenames": True,
        "logger": _Logger(),
    }
    if task.cookies:
        cookiefile = workdir / "cookies.txt"
        write_netscape_cookies(task.cookies, cookiefile)
        opts["cookiefile"] = str(cookiefile)
    ffmpeg = ffmpeg_path(settings.ffmpeg_path)
    if ffmpeg:
        ensure_executable(ffmpeg)
        opts["ffmpeg_location"] = ffmpeg
    deno = deno_path()
    if deno:
        ensure_executable(deno)
        opts["js_runtimes"] = {"deno": {"path": deno}}
    return opts


def _clean_error(e: Exception) -> str:
    msg = str(e).strip()
    for prefix in ("ERROR: ", "[generic] "):
        if msg.startswith(prefix):
            msg = msg[len(prefix):]
    return msg or e.__class__.__name__


def extract_info(task: Task, settings: Settings) -> dict:
    """只解析不下載，供「新增下載」對話框顯示標題與畫質選項。"""
    import yt_dlp

    workdir = Path(tempfile.mkdtemp(prefix="divebird-"))
    try:
        opts = _base_opts(task, settings, workdir)
        opts["skip_download"] = True
        with yt_dlp.YoutubeDL(opts) as ydl:
            try:
                info = ydl.extract_info(task.url, download=False)
            except yt_dlp.utils.DownloadError as e:
                raise DownloadError(_clean_error(e)) from None
            info = ydl.sanitize_info(info)
        if info.get("_type") == "playlist":
            entries = [e for e in (info.get("entries") or []) if e]
            if not entries:
                raise DownloadError("找不到可下載的影片")
            info = entries[0]
        return info
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _fsize(f: dict, duration: float | None) -> int:
    size = f.get("filesize") or f.get("filesize_approx")
    if not size and f.get("tbr") and duration:
        size = f["tbr"] * 125 * duration      # kbps → bytes
    return int(size or 0)


def format_choices(info: dict) -> list[tuple[str, str]]:
    """產生畫質選項：[(顯示文字, yt-dlp format 字串 / 特殊值)]"""
    formats = info.get("formats") or []
    duration = info.get("duration")
    videos = [f for f in formats if f.get("vcodec") not in (None, "none") and f.get("height")]
    audios = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]
    best_audio = max((_fsize(a, duration) for a in audios), default=0)

    def video_size(h: int) -> int:
        cands = [f for f in videos if f["height"] == h]
        best = max(cands, key=lambda f: (f.get("tbr") or 0, _fsize(f, duration)))
        size = _fsize(best, duration)
        if best.get("acodec") in (None, "none"):
            size += best_audio
        return size

    choices: list[tuple[str, str]] = []
    heights = sorted({f["height"] for f in videos}, reverse=True)
    if heights:
        top = video_size(heights[0])
        choices.append((f"最佳畫質（{heights[0]}p{_size_suffix(top)}）", "bv*+ba/b"))
        for h in heights:
            fps = max((f.get("fps") or 0) for f in videos if f["height"] == h)
            fps_txt = f"{int(fps)}fps" if fps and fps > 30 else ""
            label = f"{h}p{fps_txt}{_size_suffix(video_size(h))}"
            choices.append((label, f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b"))
    else:
        choices.append(("最佳品質（自動）", "bv*+ba/b"))
    if audios or heights:
        choices.append((f"僅音訊（M4A{_size_suffix(best_audio)}）", AUDIO_BEST))
        choices.append(("僅音訊（轉 MP3）", AUDIO_MP3))
    return choices


def _size_suffix(size: int) -> str:
    return f"，約 {human_size(size)}" if size > 0 else ""


class MediaDownloader:
    def __init__(self, task: Task, settings: Settings, *, rate_limit: int = 0,
                 on_update: Callable[[Task], None] | None = None):
        self.task = task
        self.settings = settings
        self.rate_limit = rate_limit
        self.on_update = on_update or (lambda t: None)
        self._stop = threading.Event()
        self._done_bytes = 0
        self._cur_file = None
        self._cur_downloaded = 0
        self._cur_total = 0
        self._grand_total = 0
        self._last_report = 0.0

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def run(self) -> bool:
        import yt_dlp
        from yt_dlp.utils import DownloadCancelled

        task = self.task
        Path(task.save_dir).mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix="divebird-"))
        try:
            opts = _base_opts(task, self.settings, workdir)
            if task.filename:
                stem = os.path.splitext(sanitize_filename(task.filename))[0]
                outtmpl = stem.replace("%", "%%") + ".%(ext)s"
            else:
                outtmpl = "%(title).150B.%(ext)s"
            fmt = task.media_format or self.settings.media_format
            opts.update({
                "paths": {"home": task.save_dir},
                "outtmpl": {"default": outtmpl},
                "progress_hooks": [self._hook],
                "postprocessor_hooks": [self._pp_hook],
                "continuedl": True,
                "concurrent_fragment_downloads": max(1, min(16, self.settings.connections)),
                "merge_output_format": self.settings.merge_format or "mp4",
            })
            if fmt == AUDIO_MP3:
                opts["format"] = "ba/b"
                opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                                           "preferredquality": "192"}]
            elif fmt == AUDIO_BEST:
                opts["format"] = "ba[ext=m4a]/ba/b"
            else:
                opts["format"] = fmt
            if self.rate_limit > 0:
                opts["ratelimit"] = self.rate_limit
            if "ffmpeg_location" not in opts and fmt not in (AUDIO_BEST,):
                # 沒有 ffmpeg 時無法合併影音，退而求其次下載已合併的單一檔案
                opts["format"] = "b"
                opts.pop("postprocessors", None)

            with yt_dlp.YoutubeDL(opts) as ydl:
                try:
                    info = ydl.extract_info(task.url, download=True)
                except DownloadCancelled:
                    return False
                except yt_dlp.utils.DownloadError as e:
                    if self.stopped:
                        return False
                    raise DownloadError(_clean_error(e)) from None
                if self.stopped:
                    return False
                info = info or {}
                if info.get("_type") == "playlist":
                    entries = [e for e in (info.get("entries") or []) if e]
                    info = entries[0] if entries else {}
                downloads = info.get("requested_downloads") or []
                filepath = (downloads[0].get("filepath") if downloads else None) or info.get("filepath") \
                    or ydl.prepare_filename(info)
            path = Path(filepath)
            if path.exists():
                task.filename = path.name
                task.save_dir = str(path.parent)
                task.total = task.downloaded = path.stat().st_size
            return True
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # ---------------------------------------------------------------- hooks
    def _hook(self, d: dict) -> None:
        from yt_dlp.utils import DownloadCancelled

        if self.stopped:
            raise DownloadCancelled()
        task = self.task
        status = d.get("status")
        if status == "downloading":
            if not self._grand_total:
                info = d.get("info_dict") or {}
                req = info.get("requested_formats")
                if req:
                    self._grand_total = sum(_fsize(f, info.get("duration")) for f in req)
            fn = d.get("filename")
            if fn != self._cur_file:
                self._cur_file = fn
            self._cur_downloaded = d.get("downloaded_bytes") or 0
            self._cur_total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            task.downloaded = self._done_bytes + self._cur_downloaded
            task.total = int(max(self._grand_total, self._done_bytes + self._cur_total, task.downloaded))
            task.speed = d.get("speed") or 0.0
            task.active_connections = self.settings.connections if d.get("fragment_count") else 1
            if not task.filename and fn:
                # 影像與聲音分開下載時暫存檔名帶有格式代碼（標題.f278.webm），優先用影片本身的標題
                title = (d.get("info_dict") or {}).get("title")
                task.title = task.title or str(title or Path(fn).stem)[:200]
        elif status == "finished":
            self._done_bytes += d.get("total_bytes") or d.get("downloaded_bytes") or self._cur_downloaded
            self._cur_file, self._cur_downloaded, self._cur_total = None, 0, 0
            task.downloaded = self._done_bytes
            task.speed = 0.0
        now = time.monotonic()
        if now - self._last_report >= 0.4 or status == "finished":
            self._last_report = now
            self.on_update(task)

    def _pp_hook(self, d: dict) -> None:
        if d.get("status") == "started" and d.get("postprocessor") in (
                "Merger", "FFmpegExtractAudio", "FFmpegFixupM3u8", "FFmpegVideoRemuxer", "FFmpegFixupStretched"):
            self.task.status = Status.PROCESSING
            self.task.speed = 0.0
            self.task.active_connections = 0
            self.on_update(self.task)
