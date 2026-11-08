#!/usr/bin/env bash
# 打包免安裝版：
#   Linux：dist/Divebird/Divebird ＋ dist/Divebird-<版本>-linux-<架構>.tar.gz
#   macOS：dist/Divebird.app ＋ dist/Divebird-<版本>-macos-<架構>.dmg（拖到「應用程式」即可安裝）
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || "$ROOT/scripts/setup.sh"
cd "$ROOT"

VERSION="$("$PY" -c 'import divebird; print(divebird.__version__)')"
QT_QPA_PLATFORM=offscreen "$PY" scripts/gen_icons.py
"$PY" -m PyInstaller --noconfirm --clean divebird.spec

ARCH="$(uname -m)"
if [ "$(uname -s)" = Darwin ]; then
  # 擴充功能、說明文件已由 divebird.spec 放進 Divebird.app/Contents/Resources
  APP="$ROOT/dist/Divebird.app"
  codesign --verify --deep --strict "$APP"
  DMG="$ROOT/dist/Divebird-$VERSION-macos-$ARCH.dmg"
  STAGE="$(mktemp -d)"
  trap 'rm -rf "$STAGE"' EXIT
  ditto "$APP" "$STAGE/Divebird.app"
  ln -s /Applications "$STAGE/Applications"
  rm -f "$DMG"
  hdiutil create -volname "Divebird $VERSION" -srcfolder "$STAGE" -fs HFS+ -format UDZO -ov "$DMG" >/dev/null
  echo
  echo "完成：$APP"
  echo "磁碟映像檔：$DMG"
  exit 0
fi

DIST="$ROOT/dist/Divebird"
cp -r "$ROOT/extension" "$DIST/extension"
cp "$ROOT/README.md" "$DIST/"
cp -r "$ROOT/skills" "$DIST/skills"
cp "$ROOT/assets/divebird.png" "$DIST/divebird.png"
cp "$ROOT/scripts/linux-install.sh" "$DIST/install.sh"
chmod +x "$DIST/install.sh" "$DIST/Divebird" "$DIST/_internal/tools/"*

tar -C "$ROOT/dist" -czf "$ROOT/dist/Divebird-$VERSION-linux-$ARCH.tar.gz" Divebird
echo
echo "完成：$DIST/Divebird"
echo "壓縮檔：$ROOT/dist/Divebird-$VERSION-linux-$ARCH.tar.gz"
