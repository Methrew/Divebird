#!/usr/bin/env bash
# Divebird 啟動器（Linux／macOS）：./divebird.sh [網址...]
#   1. 專案內已有環境（.venv）→ 以原始碼執行最新程式
#   2. 旁邊有打包版（dist/Divebird/Divebird；macOS 為 dist/Divebird.app）→ 直接執行
#   3. 都沒有 → 先在專案內建立自帶環境（.runtime/、.venv/）再執行
set -euo pipefail
# readlink -f 解析捷徑連結（macOS 12.3 以前的 readlink 不支援 -f，就直接用原本的路徑）
SELF="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || echo "${BASH_SOURCE[0]}")"
ROOT="$(cd "$(dirname "$SELF")" && pwd)"
PY="$ROOT/.venv/bin/python"
EXE="$ROOT/dist/Divebird/Divebird"
if [ "$(uname -s)" = Darwin ]; then
  EXE="$ROOT/dist/Divebird.app/Contents/MacOS/Divebird"
fi

if [ -x "$PY" ]; then
  exec "$PY" -m divebird "$@"
elif [ -x "$EXE" ]; then
  exec "$EXE" "$@"
fi
echo "[Divebird] 第一次執行：正在建立自帶環境，約需數分鐘…"
"$ROOT/scripts/setup.sh"
exec "$PY" -m divebird "$@"
