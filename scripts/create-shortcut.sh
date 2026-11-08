#!/usr/bin/env bash
# 建立有圖示的 Divebird 捷徑（指向專案根目錄的 divebird.sh）：
#   Linux：應用程式選單捷徑（~/.local/share/applications/divebird.desktop）
#   macOS：~/Applications/Divebird.app（可從 Launchpad、Spotlight 開啟，也能拖到 Dock）
# 打包版請改用 dist/Divebird/install.sh（Linux）或 .dmg 安裝檔（macOS）。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
chmod +x "$ROOT/divebird.sh"

if [ "$(uname -s)" = Darwin ]; then
  APP="$HOME/Applications/Divebird.app"
  ID=io.github.acer1204.divebird.source
  if [ -e "$APP" ] && [ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$APP/Contents/Info.plist" 2>/dev/null)" != "$ID" ]; then
    # 例如把打包版 Divebird.app 裝在 ~/Applications：不要把它刪掉
    echo "$APP 已經存在，而且不是這個腳本建立的捷徑，沒有覆寫。" >&2
    exit 1
  fi
  rm -rf "$APP"
  mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
  # 啟動程式只是轉給 divebird.sh；%q 會跳脫路徑中的空白等字元
  printf '#!/bin/bash\nexec %q "$@"\n' "$ROOT/divebird.sh" > "$APP/Contents/MacOS/Divebird"
  chmod +x "$APP/Contents/MacOS/Divebird"
  cp "$ROOT/assets/divebird.icns" "$APP/Contents/Resources/divebird.icns"
  cat > "$APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key><string>Divebird</string>
  <key>CFBundleIconFile</key><string>divebird</string>
  <key>CFBundleIdentifier</key><string>io.github.acer1204.divebird.source</string>
  <key>CFBundleName</key><string>Divebird</string>
  <key>CFBundleDisplayName</key><string>Divebird</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
EOF
  # 讓 Launchpad、Spotlight 立即認得新的 App
  LSREGISTER=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
  [ -x "$LSREGISTER" ] && "$LSREGISTER" -f "$APP" >/dev/null 2>&1 || true
  echo "已建立：${APP}（可從 Launchpad 或 Spotlight 開啟 Divebird，也可以拖到 Dock）"
  exit 0
fi

APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$APPS"

# .desktop 的 Exec 欄位：路徑含空白等字元時需以雙引號包住，並跳脫 " ` $ \
exec_path=$(printf '%s' "$ROOT/divebird.sh" | sed -e 's/[\\"`$]/\\&/g')
cat > "$APPS/divebird.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Divebird
Comment=下載管理員（多連線加速、影片下載）
Exec="$exec_path" %U
Icon=$ROOT/assets/divebird.png
Terminal=false
Categories=Network;FileTransfer;
StartupWMClass=Divebird
EOF
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" >/dev/null 2>&1 || true
echo "已建立應用程式選單捷徑：$APPS/divebird.desktop"
