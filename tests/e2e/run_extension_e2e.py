"""擴充功能端對端測試（需 Playwright 與 Chromium，手動執行，不在 pytest 預設範圍內）。

    .runtime/uv/uv run --no-project --with playwright python -m playwright install chromium   # 第一次
    .runtime/uv/uv run --no-project --with playwright python tests/e2e/run_extension_e2e.py [--chromium PATH]

流程：
1. 產生測試影片（mp4 + HLS），用支援 Range 的本機伺服器提供測試網頁
2. 以獨立、乾淨的設定目錄在背景啟動 Divebird（離屏模式、不跳確認視窗）
3. 啟動載入擴充功能的 Chromium，在影片上移動滑鼠 → 點懸浮按鈕 → 確認 Divebird 下載完成
4. 測試 HLS 串流選單、以及瀏覽器下載攔截
5. AI 透過 MCP 挑選擴充功能偵測到的影音，由擴充功能補上 Cookie 送出下載
截圖輸出到 tests/e2e/out/
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "out"
EXT = ROOT / "extension"
VENV_PY = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
API_PORT = 17891

GRID_CLIPS = ("ocean-wave", "city-night", "forest-walk", "promo")
PAGES = {
    "/grid.html": """<!doctype html><meta charset=utf-8><title>Dance Videos, Download The BEST Free Stock Video Footage</title>
<body style="font-family:sans-serif;background:#f1f5f9;margin:40px">
<h2>影片牆（模擬 Pexels 搜尋結果：每張卡片都是自己的影片檔，另有一部廣告）</h2>
<div style="display:grid;grid-template-columns:repeat(2,330px);gap:16px">
  <article><a style="display:block" href="/video/ocean-wave-101/"><video id=g1 src="/media/ocean-wave.mp4"
    muted loop preload=auto width=320 height=180></video></a></article>
  <article><a style="display:block" href="/video/city-night-202/"><video id=g2 src="/media/city-night.mp4"
    muted loop preload=auto width=320 height=180></video></a></article>
  <article><a style="display:block" href="/video/forest-walk-303/"><video id=g3 src="/media/forest-walk.mp4"
    muted loop preload=auto width=320 height=180></video></a></article>
  <article><a style="display:block" href="https://ads.example.com/promo"><video id=ad src="/media/promo.mp4"
    muted loop preload=auto width=320 height=180></video></a></article>
</div>
<script>for (const v of document.querySelectorAll('video')) v.play().catch(() => {});</script>
</body>""",
    "/video.html": """<!doctype html><meta charset=utf-8><title>測試影片頁</title>
<body style="font-family:sans-serif;background:#f1f5f9;margin:40px">
<h2>直接 MP4 影片</h2>
<video id=v src="/media/sample.mp4" controls muted width=640 height=360 style="background:#000"></video>
<p><a id=dl href="/media/archive.zip">下載 archive.zip（測試攔截）</a></p>
</body>""",
    "/hls.html": """<!doctype html><meta charset=utf-8><title>HLS 串流課程 第一講</title>
<body style="font-family:sans-serif;background:#f1f5f9;margin:40px">
<h2>HLS 串流（模擬 hls.js 播放器，video.src 為 blob:）</h2>
<div style="position:relative;width:640px;height:360px">
  <video id=v muted width=640 height=360 style="background:#000"></video>
  <div style="position:absolute;inset:0" title="播放器的透明遮罩層"></div>
</div>
<script>
  const ms = new MediaSource();
  document.getElementById('v').src = URL.createObjectURL(ms);
  fetch('/hls/master.m3u8').then(r => r.text()).then(t => {
    const variant = t.split('\\n').find(l => l && !l.startsWith('#'));
    return fetch('/hls/' + variant);
  });
</script>
</body>""",
}


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass    # 下載端提早關閉連線屬正常情況，不印出錯誤


class Site:
    def __init__(self, root: Path):
        self.root = root
        self.requests: list[str] = []
        site = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                path = self.path.split("?")[0]
                site.requests.append(f"{path} {self.headers.get('Range') or ''}")
                if path in PAGES:
                    body = PAGES[path].encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                f = site.root / path.lstrip("/")
                if not f.is_file():
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                data = f.read_bytes()
                ctype = {".mp4": "video/mp4", ".m3u8": "application/vnd.apple.mpegurl", ".ts": "video/mp2t",
                         ".zip": "application/zip"}.get(f.suffix, "application/octet-stream")
                start, end, status = 0, len(data) - 1, 200
                m = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range") or "")
                if m:
                    start = int(m.group(1))
                    end = min(int(m.group(2)) if m.group(2) else len(data) - 1, len(data) - 1)
                    status = 206
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(end - start + 1))
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
                if f.suffix == ".zip":
                    self.send_header("Content-Disposition", 'attachment; filename="archive.zip"')
                self.end_headers()
                try:
                    self.wfile.write(data[start:end + 1])
                except OSError:
                    pass

        self.httpd = _QuietServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, p: str) -> str:
        return f"http://127.0.0.1:{self.port}{p}"


def make_media(root: Path, ffmpeg: str):
    media = root / "media"
    hls = root / "hls"
    media.mkdir(parents=True)
    hls.mkdir()
    common = ["-hide_banner", "-loglevel", "error", "-y",
              "-f", "lavfi", "-i", "testsrc2=duration=8:size=640x360:rate=30",
              "-f", "lavfi", "-i", "sine=frequency=660:duration=8",
              "-c:v", "libx264", "-pix_fmt", "yuv420p", "-b:v", "3M", "-c:a", "aac", "-shortest"]
    subprocess.run([ffmpeg, *common, "-movflags", "+faststart", str(media / "sample.mp4")], check=True)
    subprocess.run([ffmpeg, *common, "-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod",
                    "-master_pl_name", "master.m3u8", str(hls / "index.m3u8")], check=True)
    (media / "archive.zip").write_bytes(os.urandom(3 * 1024 * 1024))
    # 影片牆用的短片：每部內容不同，且大於擴充功能記錄影音的門檻（300 KB）
    for i, name in enumerate(GRID_CLIPS):
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", f"testsrc2=duration=3:size=640x360:rate=30,hue=h={i * 90}",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-b:v", "3M", "-movflags", "+faststart",
                        str(media / f"{name}.mp4")], check=True)


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def wait_file(path: Path, timeout=40) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists() and not path.with_name(path.name + ".part").exists():
            return True
        time.sleep(0.3)
    return False


def wait_mp4(path: Path, timeout=40) -> bool:
    """等到 ffmpeg 把串接的 MPEG-TS 轉封裝成真正的 MP4（檔頭為 ftyp）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if path.read_bytes()[4:8] == b"ftyp" and not path.with_name(path.stem + ".temp.mp4").exists():
                return True
        except OSError:
            pass
        time.sleep(0.5)
    return False


def mcp_call(cfg_dir: Path, name: str, arguments: dict) -> dict:
    import urllib.request

    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{API_PORT}/mcp", data=body, method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-11-25",
        "Authorization": f"Bearer {(cfg_dir / 'mcp-token').read_text(encoding='utf-8').strip()}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())["result"]


def mcp_browser_media(cfg_dir: Path, work: Path) -> bool:
    """播放中的 sample.mp4 應出現在 list_browser_media；指定下載後由擴充功能補上資料送出（最多約 30 秒）。"""
    media_id, deadline = None, time.time() + 45
    while not media_id and time.time() < deadline:
        tabs = mcp_call(cfg_dir, "list_browser_media", {"query": "測試影片頁"})["structuredContent"]["tabs"]
        found = [m for t in tabs for m in t["media"] if m["name"] == "sample.mp4"]
        media_id = found[0]["media_id"] if found else None
        if not media_id:
            time.sleep(1)
    print("  瀏覽器影音：", media_id)
    if not media_id:
        return False
    started = mcp_call(cfg_dir, "download_browser_media", {"media_id": media_id, "filename": "AI 下載的影片"})
    task_id = started["structuredContent"]["task_id"]
    status, t0 = {}, time.time()
    while time.time() - t0 < 120:
        status = mcp_call(cfg_dir, "get_download", {"task_id": task_id, "wait_seconds": 10})["structuredContent"]
        if status["status"] in ("completed", "error"):
            break
    print(f"  MCP 下載：{status.get('status')}，{time.time() - t0:.0f} 秒，{status.get('filename')}")
    return (status.get("status") == "completed" and status.get("filename") == "AI 下載的影片.mp4"
            and sha(Path(status["path"])) == sha(work / "site/media/sample.mp4"))


def find_chromium() -> str | None:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".cache")) / "ms-playwright"
    if not base.exists():
        base = Path.home() / ".cache" / "ms-playwright"
    exe = "chrome-win64/chrome.exe" if os.name == "nt" else "chrome-linux/chrome"
    cands = sorted(base.glob(f"chromium-*/{exe}"), reverse=True)
    return str(cands[0]) if cands else None


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # 結果含中文，非 UTF-8 主控台也要能輸出
    ap = argparse.ArgumentParser()
    ap.add_argument("--chromium", default=None)
    ap.add_argument("--youtube", action="store_true", help="額外測試真實 YouTube 頁面（需要網路）")
    ap.add_argument("--app", default=None, help="測試打包後的 Divebird 執行檔（預設用 .venv 執行原始碼）")
    args = ap.parse_args()
    from playwright.sync_api import sync_playwright

    OUT.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="divebird-e2e-"))
    ffmpeg = subprocess.run([str(VENV_PY), "-c", "import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())"],
                            capture_output=True, text=True, encoding="utf-8", check=True,
                            env=dict(os.environ, PYTHONUTF8="1")).stdout.strip()
    make_media(work / "site", ffmpeg)
    site = Site(work / "site")

    # Divebird：乾淨的設定目錄
    appdata = work / "appdata"
    if sys.platform == "darwin":     # macOS 的設定資料夾在家目錄底下：以 HOME 導到暫存目錄
        cfg_dir = appdata / "Library" / "Application Support" / "Divebird"
    else:
        cfg_dir = appdata / ("Divebird" if os.name == "nt" else "divebird")
    cfg_dir.mkdir(parents=True)
    dl_dir = work / "downloads"
    (cfg_dir / "settings.json").write_text(json.dumps({
        "download_dir": str(dl_dir), "port": API_PORT, "show_dialog": False, "minimize_to_tray": False,
        "mcp_enabled": True, "mcp_confirm": "never", "mcp_allow_private": True, "mcp_share_browser_media": True,
    }), encoding="utf-8")
    env = dict(os.environ, APPDATA=str(appdata), XDG_CONFIG_HOME=str(appdata), QT_QPA_PLATFORM="offscreen")
    if sys.platform == "darwin":
        env["HOME"] = str(appdata)
    cmd = [str(Path(args.app).resolve())] if args.app else [str(VENV_PY), "-m", "divebird"]
    app = subprocess.Popen(cmd, env=env, cwd=str(ROOT))
    results: dict[str, bool] = {}
    for _ in range(60):
        try:
            import urllib.request
            urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/api/ping", timeout=1)
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    else:
        raise SystemExit("Divebird 沒有啟動")

    chromium = args.chromium or find_chromium()
    print("Chromium:", chromium)
    prefs = work / "profile" / "Default" / "Preferences"      # 瀏覽器自己下載時存到臨時資料夾，不詢問位置
    prefs.parent.mkdir(parents=True)
    prefs.write_text(json.dumps({"download": {"default_directory": str(work / "browser-downloads"),
                                              "prompt_for_download": False}}), encoding="utf-8")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(work / "profile"), headless=False, executable_path=chromium,
            args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}", "--headless=new",
                  "--autoplay-policy=no-user-gesture-required"],
            viewport={"width": 1000, "height": 640}, accept_downloads=True,
        )
        sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker", timeout=15000)
        ext_id = sw.url.split("/")[2]
        sw.evaluate(f"chrome.storage.local.set({{port: {API_PORT}}})")
        print("擴充功能 ID:", ext_id)

        # ---------------- 1. 直接 MP4：懸浮按鈕 → 直接下載
        page = ctx.new_page()
        page.goto(site.url("/video.html"))
        page.wait_for_function("document.getElementById('v').readyState >= 1")
        box = page.locator("#v").bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.wait_for_timeout(600)
        page.screenshot(path=str(OUT / "1-floating-button.png"))
        # 按鈕位於影片右上角內側 12px
        page.mouse.click(box["x"] + box["width"] - 60, box["y"] + 28)
        page.wait_for_timeout(1500)
        page.screenshot(path=str(OUT / "2-sent-toast.png"))
        ok = wait_file(dl_dir / "sample.mp4")
        results["MP4 懸浮按鈕下載"] = ok and sha(dl_dir / "sample.mp4") == sha(work / "site/media/sample.mp4")
        ranged = [r for r in site.requests if r.startswith("/media/sample.mp4 bytes=") and "bytes=0-" not in r]
        print("  多連線分段請求數：", len(ranged))

        # ---------------- 2. 下載攔截：點一般下載連結
        # Playwright 預設由 DevTools 接管下載，不會經過擴充功能的 onDeterminingFilename；改回瀏覽器原本的流程
        # （存到臨時的 browser-downloads，不詢問位置）。攔截要在瀏覽器決定存檔位置之前完成，
        # 「另存新檔」視窗才不會跳出來：記錄下載項目的變化，確認從來沒有出現檔名（filename）。
        ctx.new_cdp_session(page).send("Browser.setDownloadBehavior", {"behavior": "default"})
        sw.evaluate("() => { self.__changes = []; chrome.downloads.onChanged.addListener(d => self.__changes.push(d)); }")
        page.click("#dl")
        ok = wait_file(dl_dir / "archive.zip")
        page.wait_for_timeout(500)
        decided = [c for c in sw.evaluate("() => self.__changes") if "filename" in c]
        by_browser = list((work / "browser-downloads").glob("*"))
        print("  瀏覽器決定存檔位置：", decided or "無", "｜瀏覽器自己下載：", [f.name for f in by_browser] or "無")
        results["攔截瀏覽器下載（不跳出另存新檔）"] = (ok and sha(dl_dir / "archive.zip") == sha(work / "site/media/archive.zip")
                                       and not decided and not by_browser)

        # ---------------- 3. HLS：嗅探串流 → 選單 → 選 HLS
        page2 = ctx.new_page()
        page2.goto(site.url("/hls.html"))
        page2.wait_for_timeout(1200)
        box = page2.locator("#v").bounding_box()
        page2.mouse.move(box["x"] + 200, box["y"] + 200)
        page2.wait_for_timeout(500)
        page2.mouse.click(box["x"] + box["width"] - 60, box["y"] + 28)
        page2.wait_for_timeout(800)
        page2.screenshot(path=str(OUT / "3-hls-menu.png"))
        # 選單第一項（HLS master）位於按鈕下方
        page2.mouse.click(box["x"] + box["width"] - 200, box["y"] + 28 + 60)
        page2.wait_for_timeout(1000)
        target = dl_dir / "HLS 串流課程 第一講.mp4"
        results["HLS 串流嗅探 + 下載"] = wait_file(target, 60) and wait_mp4(target)

        # ---------------- 3b. 影片牆：網頁上很多影片時，按哪一部就直接下載哪一部，用卡片名稱存檔
        page3 = ctx.new_page()
        page3.goto(site.url("/grid.html"))
        page3.wait_for_timeout(2500)
        sniffed = sw.evaluate("() => Math.max(0, ...[...tabMedia.values()].map(l => l.length))")
        box = page3.locator("#g2").bounding_box()
        page3.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page3.wait_for_timeout(600)
        page3.mouse.click(box["x"] + box["width"] - 60, box["y"] + 28)
        page3.wait_for_timeout(1500)
        page3.screenshot(path=str(OUT / "4-grid.png"))
        target = dl_dir / "city night 202.mp4"
        print("  影片牆偵測到的影音：", sniffed)
        results["影片牆：只下載指著的那部"] = (sniffed >= 4 and wait_file(target)
                                        and sha(target) == sha(work / "site/media/city-night.mp4"))

        # ---------------- 4. AI 透過 MCP 下載瀏覽器偵測到的影音（擴充功能補上 Cookie 與 Referer 送出）
        results["AI 透過 MCP 下載瀏覽器偵測到的影音"] = mcp_browser_media(cfg_dir, work)

        # ---------------- 4.（可選）真實 YouTube：影片為 blob: 串流 → 整頁交給 yt-dlp
        if args.youtube:
            yt = ctx.new_page()
            yt.goto("https://www.youtube.com/watch?v=jNQXAC9IVRw", timeout=120000)
            yt.wait_for_selector("video", state="attached", timeout=120000)
            yt.wait_for_function("(() => { const v = document.querySelector('video');"
                                 " const r = v && v.getBoundingClientRect(); return r && r.width > 300; })()",
                                 timeout=60000)
            yt.wait_for_timeout(1500)
            box = yt.locator("video").first.bounding_box()
            yt.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            yt.wait_for_timeout(700)
            yt.screenshot(path=str(OUT / "5-youtube-button.png"))
            right = min(box["x"] + box["width"], yt.viewport_size["width"])
            yt.mouse.click(right - 60, max(box["y"], 0) + 28)
            yt.wait_for_timeout(1500)
            yt.screenshot(path=str(OUT / "6-youtube-sent.png"))
            results["YouTube 懸浮按鈕（yt-dlp）"] = wait_file(dl_dir / "Me at the zoo.mp4", 120)

        # ---------------- 5. 彈出視窗
        popup = ctx.new_page()
        popup.set_viewport_size({"width": 380, "height": 520})
        popup.goto(f"chrome-extension://{ext_id}/popup.html")
        popup.wait_for_timeout(800)
        popup.screenshot(path=str(OUT / "4-popup.png"))
        ctx.close()

    try:
        import urllib.request
        tasks = json.loads((cfg_dir / "tasks.json").read_text(encoding="utf-8")) if (cfg_dir / "tasks.json").exists() else []
        print("Divebird 任務：", [(t["filename"], t["status"], t["kind"]) for t in tasks])
    finally:
        app.terminate()
        app.wait(10)
    print()
    for k, v in results.items():
        print(("PASS " if v else "FAIL ") + k)
    print("下載目錄：", sorted(p.name for p in dl_dir.iterdir()) if dl_dir.exists() else [])
    sys.exit(0 if results and all(results.values()) else 1)


if __name__ == "__main__":
    main()
