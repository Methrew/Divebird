"""改名（OpenDM → Divebird）後，舊資料要能自動搬到新資料夾。"""
import json
import sys
from pathlib import Path

from divebird import config


def _base_env(monkeypatch, tmp_path) -> Path:
    """把設定資料夾導到暫存目錄，回傳放置設定資料夾的上層目錄。"""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(config, "app_dir", lambda: tmp_path / "app")   # 不在可攜模式
    if sys.platform == "darwin":
        return tmp_path / "Library" / "Application Support"
    return tmp_path


def _names():
    if sys.platform in ("win32", "darwin"):
        return "OpenDM", "Divebird"
    return "opendm", "divebird"


def test_legacy_data_is_copied(monkeypatch, tmp_path):
    base = _base_env(monkeypatch, tmp_path)
    old_name, new_name = _names()
    old = base / old_name
    old.mkdir(parents=True)
    (old / "tasks.json").write_text(json.dumps([{"url": "https://example.com/a.zip"}]), encoding="utf-8")
    (old / "settings.json").write_text(json.dumps({"port": 18000}), encoding="utf-8")

    path = config.data_dir()
    assert path == base / new_name
    assert json.loads((path / "tasks.json").read_text(encoding="utf-8"))[0]["url"] == "https://example.com/a.zip"
    assert config.Settings.load().port == 18000
    assert (old / "tasks.json").exists(), "舊資料夾保留當作備份"


def test_existing_new_data_is_not_overwritten(monkeypatch, tmp_path):
    base = _base_env(monkeypatch, tmp_path)
    old_name, new_name = _names()
    (base / old_name).mkdir(parents=True)
    (base / old_name / "settings.json").write_text(json.dumps({"port": 18000}), encoding="utf-8")
    (base / new_name).mkdir()
    (base / new_name / "settings.json").write_text(json.dumps({"port": 19000}), encoding="utf-8")
    config.data_dir()
    assert config.Settings.load().port == 19000


def test_failed_copy_is_retried(monkeypatch, tmp_path):
    """複製失敗時不可留下半套新資料夾，下次啟動要能重試。"""
    base = _base_env(monkeypatch, tmp_path)
    old_name, new_name = _names()
    old = base / old_name
    old.mkdir(parents=True)
    (old / "tasks.json").write_text("[]", encoding="utf-8")
    real_copytree = config.shutil.copytree
    calls = {"n": 0}

    def flaky_copytree(src, dst, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            (Path(dst)).mkdir()
            raise OSError("檔案被鎖住")
        return real_copytree(src, dst, *a, **kw)

    monkeypatch.setattr(config.shutil, "copytree", flaky_copytree)
    first = config.data_dir()        # 搬移失敗：data_dir() 仍會建立一個空的新資料夾
    assert not (first / "tasks.json").exists()
    second = config.data_dir()       # 下次啟動
    assert (second / "tasks.json").exists(), "第二次啟動應重新搬移"
    assert not second.with_name(second.name + ".migrating").exists()


def test_fresh_install_without_legacy(monkeypatch, tmp_path):
    base = _base_env(monkeypatch, tmp_path)
    _, new_name = _names()
    assert config.data_dir() == base / new_name
    assert (base / new_name).is_dir()
