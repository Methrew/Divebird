"""檢查 Divebird.app 裡的每個程式（執行檔與函式庫）是否都：

- 以同一個 Developer ID Application 憑證簽章：hardened runtime 只允許載入同一個開發者（或 Apple）簽章的
  函式庫，只要有一個不同，App 在別人的電腦上就會無法啟動，而公證並不檢查這一點；
  其他種類的憑證（例如 Apple Development）則要等到上傳後才會被公證拒絕
- 開啟 hardened runtime、帶有安全時間戳記：Apple 公證的要求

    .venv/bin/python scripts/macos_check_signatures.py dist/Divebird.app

scripts/build.sh 以 DIVEBIRD_SIGN_IDENTITY 簽章時會自動執行。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

MACHO_MAGICS = (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca")
DEVELOPER_ID = "Developer ID Application:"


def is_macho(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) in MACHO_MAGICS
    except OSError:
        return False


def signature_info(text: str) -> dict:
    """解析 `codesign -dv --verbose=2` 的輸出（codesign 印在 stderr）。"""
    team = re.search(r"^TeamIdentifier=(.*)$", text, re.M)
    authority = re.search(r"^Authority=(.*)$", text, re.M)       # 第一個 Authority 是簽章用的憑證
    flags = re.search(r"\bflags=0x[0-9a-fA-F]+\(([^)]*)\)", text)
    names = flags.group(1).split(",") if flags else []
    return {
        "team": team.group(1).strip() if team else "",
        "authority": authority.group(1).strip() if authority else "",
        "adhoc": "adhoc" in names,
        "runtime": "runtime" in names,
        "timestamp": re.search(r"^Timestamp=", text, re.M) is not None,
    }


def _codesign_info(path: Path) -> dict | None:
    r = subprocess.run(["codesign", "-dv", "--verbose=2", str(path)], capture_output=True, text=True)
    return signature_info(r.stderr) if r.returncode == 0 else None


def check(app: Path) -> list[str]:
    """回傳發現的問題（空清單表示全部符合）。"""
    top = _codesign_info(app)
    if not top or top["adhoc"] or top["team"] in ("", "not set"):
        return [f"{app.name} 沒有以 Developer ID 簽章"]
    if not top["authority"].startswith(DEVELOPER_ID):
        return [f"{app.name} 的憑證不是「Developer ID Application」（實際為 {top['authority'] or '未知'}），"
                "Apple 公證不會接受"]
    problems = []
    for root, _dirs, names in os.walk(app / "Contents"):
        for name in sorted(names):
            path = Path(root) / name
            if path.is_symlink() or not is_macho(path):
                continue
            rel = path.relative_to(app).as_posix()
            info = _codesign_info(path)
            if info is None:
                problems.append(f"{rel}：沒有簽章")
                continue
            if info["team"] != top["team"]:
                problems.append(f"{rel}：開發者不同（{info['team'] or '無'}，應為 {top['team']}）")
            elif not info["authority"].startswith(DEVELOPER_ID):
                problems.append(f"{rel}：憑證不是「Developer ID Application」（{info['authority'] or '未知'}）")
            if not info["runtime"]:
                problems.append(f"{rel}：沒有開啟 hardened runtime")
            if not info["timestamp"]:
                problems.append(f"{rel}：沒有安全時間戳記")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    app = Path(argv[1])
    problems = check(app)
    for line in problems:
        print("✗ " + line)
    if problems:
        return 1
    print(f"✓ {app.name} 的所有程式都以同一個 Developer ID 簽章，並開啟 hardened runtime 與安全時間戳記")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
