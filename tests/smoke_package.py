"""打包版煙霧測試（Windows / Linux / macOS 通用，CI 也會執行）。

    .venv/bin/python tests/smoke_package.py dist/Divebird/Divebird
    .venv/bin/python tests/smoke_package.py dist/Divebird.app/Contents/MacOS/Divebird     （macOS）

以乾淨的設定目錄、離屏模式啟動打包好的 Divebird，透過本機 API 送出：
1. 一般檔案（多連線分段下載）
2. HLS 串流（驗證內附的 ffmpeg 可合併成 mp4）
並檢查下載結果；另外驗證 MCP：HTTP 端點（/mcp）與打包好的 stdio 橋接（divebird-mcp）。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "e2e"))
from run_extension_e2e import Site, make_media  # noqa: E402

PORT = 17893


def api(path: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}",
                                 data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def wait_file(p: Path, timeout: float = 90) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if p.exists() and not p.with_name(p.name + ".part").exists():
            return True
        time.sleep(0.3)
    return False


def is_mp4(p: Path, wait: float = 0) -> bool:
    deadline = time.time() + wait
    while True:
        try:
            if p.read_bytes()[4:8] == b"ftyp" and not p.with_name(p.stem + ".temp.mp4").exists():
                return True
        except OSError:
            pass
        if time.time() >= deadline:
            return False
        time.sleep(0.5)


def check_mcp_http(token_file: Path) -> bool:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "get_status", "arguments": {}}}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/mcp", data=body, method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-11-25", "Authorization": f"Bearer {token_file.read_text().strip()}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())["result"]["structuredContent"]["app"] == "Divebird"


def check_mcp_stdio(app: Path, env: dict) -> bool:
    bridge = app.with_name("divebird-mcp.exe" if os.name == "nt" else "divebird-mcp")
    lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "smoke"}}},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_status", "arguments": {}}}]
    out = subprocess.run([str(bridge)], input="".join(json.dumps(m) + "\n" for m in lines).encode(),
                         capture_output=True, env=env, timeout=60).stdout.decode("utf-8")
    replies = {r["id"]: r for r in map(json.loads, out.splitlines())}
    return (replies[1]["result"]["serverInfo"]["name"] == "divebird"
            and replies[2]["result"]["structuredContent"]["app"] == "Divebird")


def main() -> int:
    # 結果含中文：在 cp1252 等非 UTF-8 主控台（例如 GitHub 的 Windows 主機）也要能輸出
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    app = Path(sys.argv[1]).resolve()
    work = Path(tempfile.mkdtemp(prefix="divebird-smoke-"))
    import imageio_ffmpeg

    make_media(work / "site", imageio_ffmpeg.get_ffmpeg_exe())
    site = Site(work / "site")

    cfg_root = work / "config"
    if sys.platform == "darwin":     # macOS 的設定資料夾在家目錄底下：以 HOME 導到暫存目錄
        cfg = cfg_root / "Library" / "Application Support" / "Divebird"
    else:
        cfg = cfg_root / ("Divebird" if os.name == "nt" else "divebird")
    cfg.mkdir(parents=True)
    dl = work / "downloads"
    (cfg / "settings.json").write_text(json.dumps({
        "download_dir": str(dl), "port": PORT, "show_dialog": False, "minimize_to_tray": False,
        "mcp_enabled": True, "mcp_confirm": "never",
    }), encoding="utf-8")
    env = dict(os.environ, APPDATA=str(cfg_root), XDG_CONFIG_HOME=str(cfg_root), QT_QPA_PLATFORM="offscreen")
    if sys.platform == "darwin":
        env["HOME"] = str(cfg_root)
    proc = subprocess.Popen([str(app)], env=env)
    results = {}
    try:
        for _ in range(80):
            try:
                results["API 啟動"] = api("/api/ping")["app"] == "Divebird"
                break
            except OSError:
                time.sleep(0.5)
        else:
            results["API 啟動"] = False
            return 1

        api("/api/download", {"url": site.url("/media/sample.mp4")})
        api("/api/download", {"url": site.url("/hls/master.m3u8"), "title": "串流測試"})

        mp4 = dl / "sample.mp4"
        results["一般檔案多連線下載"] = wait_file(mp4) and (
            hashlib.sha256(mp4.read_bytes()).digest()
            == hashlib.sha256((work / "site/media/sample.mp4").read_bytes()).digest())
        ranged = [r for r in site.requests if r.startswith("/media/sample.mp4 bytes=") and "bytes=0-" not in r]
        results["使用多條分段連線"] = len(ranged) >= 2
        hls = dl / "串流測試.mp4"
        # 必須是真正的 MP4（ffmpeg 轉封裝成功），而不只是串接起來的 MPEG-TS
        results["HLS 串流 + 內附 ffmpeg 轉封裝"] = wait_file(hls) and is_mp4(hls, wait=30)
        results["MCP HTTP 端點"] = check_mcp_http(cfg / "mcp-token")
        results["MCP stdio 橋接（divebird-mcp）"] = check_mcp_stdio(app, env)
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
    for k, v in results.items():
        print(("PASS " if v else "FAIL ") + k)
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
