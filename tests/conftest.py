"""測試用 HTTP 伺服器：可切換是否支援 Range、可節流、可記錄請求。"""
from __future__ import annotations

import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class FileServer:
    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.supports_range = True
        self.throttle = 0.0               # 每 64KB 的延遲秒數
        self.slow_ranges_from: int | None = None   # 從此位移開始的 Range 請求額外變慢
        self.content_disposition: str | None = None
        self.content_types: dict[str, str] = {}     # 個別檔案的 Content-Type（預設 application/octet-stream）
        self.max_conns: int | None = None    # 模擬限制同時連線數的伺服器：超過就回 503
        self.require_range = False           # 模擬只接受 Range 請求的 CDN（例如 Bilibili）：沒有 Range 就回 567
        self.active = 0
        self.requests: list[tuple[str, str | None]] = []
        self.lock = threading.Lock()
        fs = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                name = self.path.split("?")[0]
                rng = self.headers.get("Range")
                with fs.lock:
                    fs.requests.append((name, rng))
                    fs.active += 1
                    over = fs.max_conns is not None and fs.active > fs.max_conns
                try:
                    if fs.require_range and not rng:
                        self.send_response(567)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    if over:
                        self.send_response(503)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    self._serve(name, rng)
                finally:
                    with fs.lock:
                        fs.active -= 1

            def _serve(self, name, rng):
                data = fs.files.get(name)
                if data is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                start, end, status = 0, len(data) - 1, 200
                m = re.match(r"bytes=(\d+)-(\d*)", rng or "")
                if fs.supports_range and m:
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else len(data) - 1
                    end = min(end, len(data) - 1)
                    status = 206
                self.send_response(status)
                self.send_header("Content-Type", fs.content_types.get(name, "application/octet-stream"))
                self.send_header("Content-Length", str(end - start + 1))
                if fs.supports_range:
                    self.send_header("Accept-Ranges", "bytes")
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
                if fs.content_disposition:
                    self.send_header("Content-Disposition", fs.content_disposition)
                self.end_headers()
                slow = fs.slow_ranges_from is not None and status == 206 and start == fs.slow_ranges_from
                pos = start
                try:
                    while pos <= end:
                        chunk = data[pos:min(end + 1, pos + 65536)]
                        self.wfile.write(chunk)
                        pos += len(chunk)
                        delay = fs.throttle * (8 if slow else 1)
                        if delay:
                            time.sleep(delay)
                except (ConnectionError, OSError):
                    pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.handle_error = lambda request, client_address: None  # 下載端提早關閉連線屬正常
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.port}{name}"

    def add(self, name: str, size: int) -> bytes:
        data = os.urandom(size)
        self.files[name] = data
        return data

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    fs = FileServer()
    yield fs
    fs.close()
