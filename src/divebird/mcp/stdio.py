"""MCP 的 stdio 橋接：給只能以 stdio 啟動本機 MCP 伺服器的 AI 應用程式使用。

    divebird-mcp            （免安裝版，與 Divebird 主程式放在同一個資料夾）
    python -m divebird.mcp  （從原始碼執行）

- 握手（initialize／server/discover）與 tools/list 在本地直接回答：不必等 Divebird 啟動，
  也就不會被 AI 應用程式的啟動逾時卡住
- tools/call 才轉送到 Divebird 的 http://127.0.0.1:<埠>/mcp（埠號與存取權杖從 Divebird 的設定資料夾讀取，
  AI 應用程式的設定檔裡不必放權杖）；Divebird 沒在執行就在背景啟動它
- stdout 只輸出 MCP 訊息（每行一則 JSON），記錄一律寫到 stderr
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import __version__
from ..config import Settings
from ..localport import listening
from . import token
from .protocol import CallContext, Endpoint
from .schema import INSTRUCTIONS, TOOL_DEFINITIONS

START_TIMEOUT = 40          # 冷啟動 Divebird（含第一次載入 Qt）最多等待秒數
CALL_TIMEOUT = 90           # 單一工具呼叫（get_download 最多等 25 秒）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))    # 連本機不走系統代理


def _log(message: str) -> None:
    print(f"[divebird-mcp] {message}", file=sys.stderr, flush=True)


def _error(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


# ---------------------------------------------------------------------- 啟動 Divebird
def launch_command() -> list[str] | None:
    """以 --minimized 在背景啟動 Divebird 的指令（找不到主程式時回傳 None）。"""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).with_name("Divebird.exe" if os.name == "nt" else "Divebird")
        if not exe.is_file():
            return None
        bundle = exe.parent.parent.parent          # macOS：Divebird.app/Contents/MacOS/Divebird
        if sys.platform == "darwin" and bundle.suffix == ".app":
            # 經由 LaunchServices 啟動，Divebird 才是獨立的 App：不會被當成 AI 應用程式的子程序，
            # 存取「下載項目」等資料夾的權限詢問也會以 Divebird 的名義出現
            return ["/usr/bin/open", "-g", "-a", str(bundle), "--args", "--minimized"]
        return [str(exe), "--minimized"]
    if os.name == "nt":
        root = Path(__file__).resolve().parents[3]
        launcher = root / "Divebird.exe"           # 從原始碼執行時的啟動程式（會先檢查並更新執行環境）
        if launcher.is_file() and (root / "Divebird.bat").is_file():
            return [str(launcher), "--minimized"]
        gui = Path(sys.prefix) / "Scripts" / "divebird-gui.exe"
        if gui.is_file():
            return [str(gui), "-m", "divebird", "--minimized"]
    return [sys.executable, "-m", "divebird", "--minimized"]


def _spawn(cmd: list[str]) -> None:
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                  close_fds=True, cwd=str(Path(cmd[0]).parent))
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            # AI 應用程式常把 stdio 伺服器放進「結束時一併終止」的 job；讓 Divebird 脫離它，
            # 否則關掉 AI 應用程式時 Divebird 與進行中的下載也會被一起關掉
            subprocess.Popen(cmd, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kwargs)
            return
        except OSError:
            pass        # 所在的 job 不允許脫離：照常啟動
        subprocess.Popen(cmd, creationflags=flags, **kwargs)
    else:
        subprocess.Popen(cmd, start_new_session=True, **kwargs)


def _ping(port: int) -> bool:
    if not listening(port):
        return False
    try:
        with _OPENER.open(f"http://127.0.0.1:{port}/api/ping", timeout=3) as r:
            return json.loads(r.read()).get("app") == "Divebird"
    except (OSError, ValueError, AttributeError):
        return False


def ensure_running(port: int) -> str | None:
    """Divebird 沒在執行就啟動它；回傳錯誤訊息，或 None 表示可以連線。"""
    if _ping(port):
        return None
    cmd = launch_command()
    if not cmd:
        return "找不到 Divebird 主程式，請手動啟動 Divebird。"
    _log("Divebird 沒有在執行，正在背景啟動：" + " ".join(cmd))
    try:
        _spawn(cmd)
    except OSError as e:
        return f"無法啟動 Divebird：{e}"
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        if _ping(port):
            return None
        time.sleep(0.3)
    return (f"Divebird 已嘗試啟動，但 {START_TIMEOUT} 秒內沒有回應。"
            "請確認 Divebird 可以正常開啟，且設定中的埠號沒有被其他程式佔用。")


# ---------------------------------------------------------------------- 轉送工具呼叫
class ProxyTools:
    def definitions(self) -> list[dict]:
        return json.loads(json.dumps(TOOL_DEFINITIONS))

    def call(self, name: str, arguments: dict, ctx: CallContext) -> dict:
        port = Settings.load().port
        problem = ensure_running(port)
        if problem:
            return _error(problem)
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": name, "arguments": arguments}}).encode("utf-8")
        request = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", data=body, method="POST", headers={
            "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25", "Authorization": f"Bearer {token.load()}",
            "User-Agent": f"{ctx.client}/stdio",
        })
        try:
            with _OPENER.open(request, timeout=CALL_TIMEOUT) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                message = json.loads(e.read()).get("error", {}).get("message") or f"HTTP {e.code}"
            except ValueError:
                message = f"HTTP {e.code}"
            return _error(message)
        except (OSError, ValueError) as e:
            return _error(f"無法連線到 Divebird：{e}")
        if "error" in data:
            return _error(str(data["error"].get("message") or data["error"]))
        result = dict(data.get("result") or {})
        result.pop("resultType", None)
        result.pop("_meta", None)
        return result


# ---------------------------------------------------------------------- 主迴圈
def main() -> int:
    endpoint = Endpoint(ProxyTools(), name="divebird", title="Divebird", version=__version__,
                        instructions=INSTRUCTIONS)
    out = sys.stdout.buffer
    if hasattr(sys.stderr, "reconfigure"):    # 規格要求記錄也用 UTF-8（Windows 預設是系統代碼頁，例如 cp950）
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    write_lock = threading.Lock()
    client = {"name": ""}

    def respond(line: bytes) -> None:
        reply = endpoint.handle({"user-agent": f"{client['name']}/stdio"} if client["name"] else {},
                                line, stdio=True)
        if reply.body is None:
            return
        data = json.dumps(reply.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        with write_lock:
            out.write(data)
            out.flush()

    _log(f"Divebird {__version__} MCP stdio 橋接已啟動")
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="mcp-call") as pool:
        for raw in sys.stdin.buffer:
            line = raw.strip()
            if not line:
                continue
            try:      # 舊世代的 initialize 帶有用戶端名稱：之後的請求都沒有，先記下來顯示在確認視窗
                msg = json.loads(line)
                if isinstance(msg, dict) and msg.get("method") == "initialize":
                    info = (msg.get("params") or {}).get("clientInfo") or {}
                    client["name"] = str(info.get("title") or info.get("name") or "")[:60]
            except ValueError:
                pass
            pool.submit(respond, line)
    return 0
