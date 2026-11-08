from __future__ import annotations

import mimetypes
import os
import re
import subprocess
import sys
from email.message import Message
from pathlib import Path
from urllib.parse import unquote, urlsplit

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
MAX_NAME_BYTES = 200


def sanitize_filename(name: str, fallback: str = "download") -> str:
    """產生在 Windows 與 Linux 皆合法的檔名。"""
    name = _ILLEGAL.sub("_", name or "").strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    if not name:
        name = fallback
    stem, ext = os.path.splitext(name)
    if stem.upper() in _RESERVED:
        stem = f"_{stem}"
    # 以 UTF-8 位元組長度截斷（Linux 檔名上限 255 bytes，中文一字 3 bytes）
    ext = ext[:20]
    budget = MAX_NAME_BYTES - len(ext.encode("utf-8"))
    raw = stem.encode("utf-8")
    if len(raw) > budget:
        stem = raw[:budget].decode("utf-8", errors="ignore").rstrip()
    return stem + ext


def unique_path(path: Path, extra_suffixes: tuple[str, ...] = (".part",)) -> Path:
    """若檔案（或其暫存檔）已存在，於檔名後加上 (1)、(2)…"""

    def taken(p: Path) -> bool:
        return p.exists() or any(p.with_name(p.name + s).exists() for s in extra_suffixes)

    if not taken(path):
        return path
    stem, ext = path.stem, path.suffix
    i = 1
    while True:
        candidate = path.with_name(f"{stem} ({i}){ext}")
        if not taken(candidate):
            return candidate
        i += 1


def filename_from_content_disposition(value: str | None) -> str:
    if not value:
        return ""
    msg = Message()
    msg["content-disposition"] = value
    # get_filename 會處理 RFC 2231/5987 的 filename*=UTF-8''... 格式
    name = msg.get_filename() or ""
    if name and "%" in name and "filename*" not in value.lower():
        name = unquote(name)
    try:
        # 部分伺服器直接送出 UTF-8 位元組，被以 latin-1 解讀
        name = name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return os.path.basename(name.replace("\\", "/"))


def filename_from_url(url: str) -> str:
    path = urlsplit(url).path
    return unquote(path.rsplit("/", 1)[-1]) if path else ""


def ensure_extension(name: str, content_type: str | None) -> str:
    if os.path.splitext(name)[1] or not content_type:
        return name
    ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ""
    return name + ext


def human_size(n: float) -> str:
    if n is None or n < 0:
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def human_time(sec: float) -> str:
    if sec is None or sec < 0:
        return "-"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def open_path(path: Path, select: bool = False) -> None:
    """以系統預設程式開啟檔案，或在檔案總管中顯示。"""
    path = Path(path)
    try:
        if sys.platform == "win32":
            if select and path.exists():
                subprocess.Popen(["explorer", "/select,", str(path)])
            else:
                os.startfile(str(path if not select else path.parent))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            # open -R：在 Finder 中顯示並選取檔案
            cmd = ["open", "-R", str(path)] if select and path.exists() else \
                ["open", str(path.parent if select else path)]
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            target = path.parent if select else path
            subprocess.Popen(["xdg-open", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def write_netscape_cookies(cookies: list[dict], path: Path) -> None:
    """將擴充功能傳來的 chrome.cookies 物件寫成 Netscape cookies.txt（yt-dlp 使用）。"""
    lines = ["# Netscape HTTP Cookie File"]
    for c in cookies:
        domain = c.get("domain", "")
        if not domain:
            continue
        host_only = c.get("hostOnly", not domain.startswith("."))
        if not host_only and not domain.startswith("."):
            domain = "." + domain
        flag = "FALSE" if host_only else "TRUE"
        secure = "TRUE" if c.get("secure") else "FALSE"
        expires = int(c.get("expirationDate") or 0)
        name, value = str(c.get("name", "")), str(c.get("value", ""))
        if c.get("httpOnly"):
            domain = "#HttpOnly_" + domain
        lines.append("\t".join([domain, flag, c.get("path") or "/", secure, str(expires), name, value]))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def cookies_to_jar(cookies: list[dict]):
    import requests

    jar = requests.cookies.RequestsCookieJar()
    for c in cookies:
        domain = c.get("domain", "")
        if not c.get("hostOnly", True) and domain and not domain.startswith("."):
            domain = "." + domain
        jar.set(str(c.get("name", "")), str(c.get("value", "")), domain=domain, path=c.get("path") or "/",
                secure=bool(c.get("secure")))
    return jar
