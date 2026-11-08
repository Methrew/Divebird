"""「關於」視窗與檢查更新（GitHub 上的最新正式版本）。"""
import json
import os
import time

import pytest

from divebird import __version__, about


def test_version_compare():
    assert about.parse_version("v1.2.0") == (1, 2, 0) and about.parse_version("1.10") == (1, 10, 0)
    assert about.parse_version("v1.2.0-beta") is None and about.parse_version("") is None
    assert about.is_newer("v1.2.0", "1.1.0") and about.is_newer("v1.10.0", "1.9.3")
    assert not about.is_newer("v1.1.0", "1.1.0") and not about.is_newer("v1.0.9", "1.1.0")
    assert not about.is_newer("nightly", "1.1.0")


def _release(server, monkeypatch, payload, status_path="/latest"):
    server.files[status_path] = json.dumps(payload).encode()
    server.content_types[status_path] = "application/json"
    monkeypatch.setattr(about, "LATEST_API", server.url("/latest"))


def test_latest_release(server, monkeypatch):
    page = about.REPO_URL + "/releases/tag/macos-v9.8.7"
    # 同一個專案還有 Windows／Linux 的 v* 版本、草稿與預先發行版，都要略過
    _release(server, monkeypatch, [
        {"tag_name": "v99.0.0", "html_url": about.REPO_URL + "/releases/tag/v99.0.0"},
        {"tag_name": "macos-v99.0.0", "draft": True, "html_url": page},
        {"tag_name": "macos-v98.0.0", "prerelease": True, "html_url": page},
        {"tag_name": "macos-v1.0.0", "html_url": about.REPO_URL + "/releases/tag/macos-v1.0.0"},
        {"tag_name": "macos-v9.8.7", "html_url": page},
    ])
    assert about.latest_release() == {"version": "9.8.7", "url": page}
    # 回應裡的網址不是本專案的頁面時，改開 Releases 頁面
    _release(server, monkeypatch, [{"tag_name": "macos-v9.8.7", "html_url": "https://evil.example.com/"}])
    assert about.latest_release()["url"] == about.RELEASES_URL
    _release(server, monkeypatch, [{"tag_name": "v9.8.7"}, {"tag_name": "macos-vlatest"}])
    with pytest.raises(about.UpdateCheckError, match="還沒有 macOS"):
        about.latest_release()
    _release(server, monkeypatch, {"message": "rate limited"})
    with pytest.raises(about.UpdateCheckError, match="無法辨識"):
        about.latest_release()
    monkeypatch.setattr(about, "LATEST_API", server.url("/missing"))
    with pytest.raises(about.UpdateCheckError, match="還沒有正式版本"):
        about.latest_release()
    monkeypatch.setattr(about, "LATEST_API", "http://127.0.0.1:1/latest")
    with pytest.raises(about.UpdateCheckError, match="連不上"):
        about.latest_release(timeout=2)


def test_about_dialog(monkeypatch):
    """版本、作者、專案網址與授權；檢查更新：有新版時詢問，按「是」開啟 Release 頁面。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import QApplication, QLabel, QMessageBox

    from divebird.gui.dialogs import AboutDialog

    app = QApplication.instance() or QApplication([])
    dlg = AboutDialog()
    text = " ".join(label.text() for label in dlg.findChildren(QLabel))
    for expected in (__version__, about.AUTHOR_URL, about.REPO_URL, about.LICENSE_URL, "MIT License"):
        assert expected in text

    opened, asked, shown = [], [], []
    answer = {"yes": True}
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))
    monkeypatch.setattr(AboutDialog, "_ask_update", lambda self, version: asked.append(version) or answer["yes"])
    monkeypatch.setattr(QMessageBox, "information", lambda parent, title, message: shown.append(message))
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, message: shown.append(message))

    def check(result):
        def fake():
            if isinstance(result, Exception):
                raise result
            return result
        monkeypatch.setattr(about, "latest_release", fake)
        dlg.check_updates()
        assert not dlg.check_btn.isEnabled()
        deadline = time.monotonic() + 10
        while not dlg.check_btn.isEnabled() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert dlg.check_btn.isEnabled() and dlg.check_btn.text() == "檢查更新"

    newer = {"version": "99.0.0", "url": about.REPO_URL + "/releases/tag/v99.0.0"}
    check(newer)
    assert asked == ["99.0.0"] and opened == [newer["url"]]
    answer["yes"] = False                      # 按「否」：不開啟網頁
    check(newer)
    assert len(asked) == 2 and len(opened) == 1
    check({"version": __version__, "url": about.RELEASES_URL})
    assert "已經是最新版本" in shown[-1] and len(asked) == 2
    check(about.UpdateCheckError("連不上 GitHub，請檢查網路連線。"))
    assert "無法檢查更新" in shown[-1] and "連不上 GitHub" in shown[-1]
    dlg.deleteLater()
    app.processEvents()
