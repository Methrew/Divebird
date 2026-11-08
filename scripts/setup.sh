#!/usr/bin/env bash
# Divebird 開發環境建置（Linux／macOS）
# 所有東西都放在專案目錄內，不依賴、也不修改系統上的 Python：
#   .runtime/uv/       uv 套件管理器（自動下載）
#   .runtime/python/   獨立的 CPython 3.12（由 uv 下載）
#   .runtime/cache/    套件快取
#   .venv/             專案虛擬環境（PySide6、yt-dlp、ffmpeg、deno…）
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME="$ROOT/.runtime"
UV="$RUNTIME/uv/uv"

if [ ! -x "$UV" ]; then
  echo "==> 下載 uv（Python 環境管理器）..."
  mkdir -p "$RUNTIME/uv"
  case "$(uname -m)" in
    x86_64|amd64) ARCH=x86_64 ;;
    aarch64|arm64) ARCH=aarch64 ;;
    *) echo "不支援的 CPU 架構：$(uname -m)"; exit 1 ;;
  esac
  case "$(uname -s)" in
    Darwin) TARGET="$ARCH-apple-darwin" ;;
    *) TARGET="$ARCH-unknown-linux-gnu" ;;
  esac
  URL="https://github.com/astral-sh/uv/releases/latest/download/uv-$TARGET.tar.gz"
  if command -v curl >/dev/null; then curl -fsSL "$URL" -o "$RUNTIME/uv/uv.tar.gz"
  else wget -qO "$RUNTIME/uv/uv.tar.gz" "$URL"; fi
  tar -xzf "$RUNTIME/uv/uv.tar.gz" -C "$RUNTIME/uv" --strip-components=1
  rm -f "$RUNTIME/uv/uv.tar.gz"
fi

export UV_PYTHON_INSTALL_DIR="$RUNTIME/python"
export UV_CACHE_DIR="$RUNTIME/cache"
export UV_PYTHON_PREFERENCE=only-managed
export UV_PROJECT_ENVIRONMENT="$ROOT/.venv"

echo "==> 安裝獨立 Python 與相依套件（第一次約需數分鐘）..."
"$UV" sync --project "$ROOT"

# Qt 的 xcb 平台外掛在部分發行版需要額外的系統函式庫（macOS 不需要）
if [ "$(uname -s)" = Linux ] && ! ldconfig -p 2>/dev/null | grep -q libxcb-cursor.so.0; then
  echo
  echo "提醒：系統缺少 libxcb-cursor0，圖形介面可能無法啟動，請安裝："
  echo "  Debian/Ubuntu: sudo apt install libxcb-cursor0"
  echo "  Fedora:        sudo dnf install xcb-util-cursor"
  echo "  Arch:          sudo pacman -S xcb-util-cursor"
fi

echo
echo "完成！啟動方式：  ./divebird.sh"
