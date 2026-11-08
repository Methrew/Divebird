#!/usr/bin/env bash
# 打包免安裝版：
#   Linux：dist/Divebird/Divebird ＋ dist/Divebird-<版本>-linux-<架構>.tar.gz
#   macOS：dist/Divebird.app ＋ dist/Divebird-<版本>-macos-<架構>.dmg（拖到「應用程式」即可安裝）
#
# macOS 版要分享給別人時，以 Developer ID 簽章並送 Apple 公證（需要付費的 Apple 開發者帳號，見 README）：
#   DIVEBIRD_SIGN_IDENTITY="Developer ID Application: 名字 (TEAMID)"   以這個憑證簽章（hardened runtime）
#   DIVEBIRD_NOTARY_PROFILE=divebird-notary   以 `xcrun notarytool store-credentials` 儲存的設定送公證
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || "$ROOT/scripts/setup.sh"
cd "$ROOT"

SIGN_ID="${DIVEBIRD_SIGN_IDENTITY:-}"
NOTARY="${DIVEBIRD_NOTARY_PROFILE:-}"
if [ "$(uname -s)" = Darwin ]; then
  # 打包要好幾分鐘：設定有問題就先停下來
  if [ -n "$NOTARY" ] && [ -z "$SIGN_ID" ]; then
    echo "公證需要 Developer ID 簽章：請同時設定 DIVEBIRD_SIGN_IDENTITY" >&2
    exit 1
  fi
  if [ -n "$SIGN_ID" ] && ! security find-identity -v -p codesigning | grep -qF -- "$SIGN_ID"; then
    echo "鑰匙圈裡找不到簽章憑證「${SIGN_ID}」（列出可用的憑證：security find-identity -v -p codesigning）" >&2
    exit 1
  fi
fi

VERSION="$("$PY" -c 'import divebird; print(divebird.__version__)')"
QT_QPA_PLATFORM=offscreen "$PY" scripts/gen_icons.py
"$PY" -m PyInstaller --noconfirm --clean divebird.spec     # divebird.spec 會讀取 DIVEBIRD_SIGN_IDENTITY

ARCH="$(uname -m)"
if [ "$(uname -s)" = Darwin ]; then
  # 擴充功能、說明文件已由 divebird.spec 放進 Divebird.app/Contents/Resources
  APP="$ROOT/dist/Divebird.app"
  DMG="$ROOT/dist/Divebird-$VERSION-macos-$ARCH.dmg"
  STAGE="$(mktemp -d)"
  WORK="$(mktemp -d)"
  trap 'rm -rf "$STAGE" "$WORK"' EXIT

  sign() { codesign --force --sign "$SIGN_ID" --options runtime --timestamp "$@"; }
  notarize() {   # 送 Apple 公證並等待結果（約需數分鐘）；沒有通過時印出 Apple 的說明
    local result state id
    result="$(xcrun notarytool submit "$1" --keychain-profile "$NOTARY" --wait --output-format json || true)"
    state="$(printf '%s' "$result" | "$PY" -c 'import json, sys; print(json.loads(sys.stdin.read() or "{}").get("status", ""))' || true)"
    if [ "$state" != Accepted ]; then
      echo "公證沒有通過（${state:-無法送出}）" >&2
      id="$(printf '%s' "$result" | "$PY" -c 'import json, sys; print(json.loads(sys.stdin.read() or "{}").get("id", ""))' || true)"
      [ -z "$id" ] || xcrun notarytool log "$id" --keychain-profile "$NOTARY" >&2 || true
      exit 1
    fi
  }

  if [ -n "$SIGN_ID" ]; then
    # 內附的 deno（yt-dlp 解析 YouTube 用的 JavaScript 引擎）在 hardened runtime 下需要 JIT 權限：
    # 帶著這項權限重新簽章，再重簽外層的 App（裡面的檔案變了，App 的簽章要跟著更新）
    sign --entitlements "$ROOT/scripts/macos-deno-entitlements.plist" "$APP/Contents/Frameworks/tools/deno"
    sign "$APP"
    "$PY" scripts/macos_check_signatures.py "$APP"
  fi
  codesign --verify --deep --strict "$APP"
  if [ -n "$NOTARY" ]; then
    echo "==> 送 Apple 公證 Divebird.app（約需數分鐘）..."
    ditto -c -k --keepParent "$APP" "$WORK/Divebird.zip"
    notarize "$WORK/Divebird.zip"
    xcrun stapler staple "$APP"     # 公證結果附在 App 上：使用者沒有網路時也能通過檢查
  fi

  ditto "$APP" "$STAGE/Divebird.app"
  ln -s /Applications "$STAGE/Applications"
  rm -f "$DMG"
  # GitHub Actions 的 macOS 主機偶爾會出現「hdiutil: create failed - Resource busy」：重試幾次
  for attempt in 1 2 3; do
    if hdiutil create -volname "Divebird $VERSION" -srcfolder "$STAGE" -fs HFS+ -format UDZO -ov "$DMG" >/dev/null; then
      break
    fi
    [ "$attempt" = 3 ] && exit 1
    echo "hdiutil 失敗，3 秒後重試…"
    sleep 3
  done
  if [ -n "$SIGN_ID" ]; then
    codesign --force --sign "$SIGN_ID" --timestamp "$DMG"
  fi
  if [ -n "$NOTARY" ]; then
    echo "==> 送 Apple 公證磁碟映像檔..."
    notarize "$DMG"
    xcrun stapler staple "$DMG"
    # 以 Gatekeeper 的發佈規則再檢查一次（macOS 14 以上才有這個工具）
    if command -v syspolicy_check >/dev/null; then syspolicy_check distribution "$APP"; fi
  fi
  echo
  echo "完成：$APP"
  echo "磁碟映像檔：$DMG"
  if [ -n "$NOTARY" ]; then
    echo "已用 Developer ID 簽章並通過 Apple 公證，可以分享給其他人。"
  elif [ -n "$SIGN_ID" ]; then
    echo "已用 Developer ID 簽章，但沒有公證：分享給別人前，請設定 DIVEBIRD_NOTARY_PROFILE 送公證。"
  else
    echo "只有 ad-hoc 簽章：分享給別人時，對方的 Mac 會擋下（可能顯示「已損毀」），見 README。"
  fi
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
