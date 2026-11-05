"""以 ffmpeg 產生本機 HLS 串流，驗證 yt-dlp 下載流程與內附工具。"""
import subprocess
import time
from pathlib import Path

import pytest

from divebird.config import Settings
from divebird.engine.manager import DownloadManager
from divebird.engine.media_engine import (
    MediaDownloader, extract_info, format_choices, is_manifest_url, is_media_site,
)
from divebird.engine.tools import deno_path, ffmpeg_path
from divebird.models import Kind, Status, Task


def test_bundled_tools_found():
    assert ffmpeg_path() and Path(ffmpeg_path()).is_file()
    assert deno_path() and Path(deno_path()).is_file()


def test_ffmpeg_can_demux_mpegts(tmp_path):
    """HLS 片段是 MPEG-TS；Linux 上靜態 ffmpeg 解析 TS 曾因系統 gconv 模組不相容而 segfault。"""
    ff = ffmpeg_path()
    ts = tmp_path / "a.ts"
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=1",
                    "-c:v", "libx264", "-f", "mpegts", str(ts)], check=True, timeout=60)
    r = subprocess.run([ff, "-hide_banner", "-i", str(ts), "-c", "copy", "-f", "mp4", "-y",
                        str(tmp_path / "a.mp4")], capture_output=True, timeout=60)
    assert r.returncode == 0, r.stderr[-500:]
    assert (tmp_path / "a.mp4").read_bytes()[4:8] == b"ftyp"


def test_site_detection():
    assert is_media_site("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert is_media_site("https://www.bilibili.com/video/BV1GJ411x7h7")
    assert not is_media_site("https://example.com/files/video.mp4")
    # 影音網站的圖床：yt-dlp 的 imgur 解析器會認領，但靜態圖片要直接下載
    assert is_media_site("https://imgur.com/Q1B0UKR") and is_media_site("https://i.imgur.com/Q1B0UKR.gifv")
    for url in ("https://i.imgur.com/Q1B0UKR.png", "https://i.imgur.com/zDkk9sV.JPG?1", "https://i.imgur.com/a.webp"):
        assert not is_media_site(url), url
    from divebird.intake import task_from_payload
    assert task_from_payload({"url": "https://i.imgur.com/Q1B0UKR.png"}, Settings()).kind == Kind.HTTP
    assert is_manifest_url("https://cdn.example.com/live/master.m3u8?token=1")
    assert is_manifest_url("https://cdn.example.com/a/manifest.mpd")
    assert not is_manifest_url("https://cdn.example.com/a/video.mp4")


@pytest.fixture
def hls_server(server, tmp_path):
    src = tmp_path / "hls"
    src.mkdir()
    cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", "testsrc=duration=4:size=320x240:rate=25",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
           "-f", "hls", "-hls_time", "1", "-hls_playlist_type", "vod",
           "-master_pl_name", "master.m3u8", str(src / "index.m3u8")]
    subprocess.run(cmd, check=True, timeout=60)
    for f in src.iterdir():
        server.files[f"/hls/{f.name}"] = f.read_bytes()
    return server


def test_range_only_sites_are_downloaded_in_chunks(server, tmp_path):
    """Bilibili 的 CDN 不接受沒有 Range 的請求（HTTP 567）：從這些網站下載時，每個請求都要帶 Range。"""
    from divebird.engine.media_engine import _base_opts, needs_range_requests

    assert needs_range_requests("https://www.bilibili.com/video/BV1kbxD61EjQ/?vd_source=1")
    assert needs_range_requests("https://b23.tv/abc") and needs_range_requests("https://m.bilibili.com/video/x")
    assert not needs_range_requests("https://notbilibili.com/x") and not needs_range_requests("https://youtu.be/x")
    youtube = _base_opts(Task(url="https://www.youtube.com/watch?v=x", kind=Kind.MEDIA), Settings(), tmp_path)
    assert "http_chunk_size" not in youtube

    import yt_dlp

    # 解析網頁之後的實際下載：影音檔放在只接受 Range 的 CDN 上（就像 Bilibili 的 .m4s 串流）
    data = server.add("/video.m4s", 3 * 1024 * 1024)
    server.require_range = True
    opts = _base_opts(Task(url="https://www.bilibili.com/video/BV1kbxD61EjQ/", kind=Kind.MEDIA), Settings(), tmp_path)
    opts["outtmpl"] = str(tmp_path / "clip.%(ext)s")
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.process_ie_result({"id": "clip", "title": "clip", "ext": "mp4", "url": server.url("/video.m4s")},
                              download=True)
    assert (tmp_path / "clip.mp4").read_bytes() == data
    assert server.requests and all(rng for _name, rng in server.requests)


def test_title_comes_from_video_not_temp_file(tmp_path):
    """影像與聲音分開下載時，暫存檔名帶有格式代碼（標題.f278.webm）：標題要用影片本身的標題。"""
    task = Task(url="https://example.com/watch?v=1", kind=Kind.MEDIA, save_dir=str(tmp_path))
    dl = MediaDownloader(task, Settings(download_dir=str(tmp_path)), on_update=lambda t: None)
    dl._hook({"status": "downloading", "filename": str(tmp_path / "Me at the zoo.f278.webm"),
              "downloaded_bytes": 10, "total_bytes": 100, "info_dict": {"title": "Me at the zoo"}})
    assert task.title == "Me at the zoo"


def test_hls_download(hls_server, tmp_path):
    out = tmp_path / "out"
    settings = Settings(download_dir=str(out), connections=4)
    task = Task(url=hls_server.url("/hls/master.m3u8"), kind=Kind.MEDIA, save_dir=str(out),
                filename="測試串流.mp4")

    info = extract_info(task, settings)
    labels = [label for label, _ in format_choices(info)]
    assert any("240p" in label for label in labels)

    updates = []
    dl = MediaDownloader(task, settings, on_update=lambda t: updates.append(t.downloaded))
    assert dl.run() is True
    result = out / task.filename
    assert result.exists() and result.suffix == ".mp4"
    assert result.stat().st_size > 10_000
    assert task.filename == "測試串流.mp4"
    assert updates, "應回報進度"
    # 輸出為合法 mp4：ffmpeg 可讀取且含影片與音訊
    probe = subprocess.run([ffmpeg_path(), "-hide_banner", "-i", str(result)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    assert "Video: h264" in probe.stderr and "Audio: aac" in probe.stderr


def test_extensionless_manifest_url_becomes_video(hls_server, tmp_path):
    """沒有 .m3u8 副檔名、伺服器也只回 octet-stream 的串流網址（例如 /master?id=1）：
    即使以一般檔案加入（沒有經過確認視窗），也要改用影音引擎下載成影片，而不是存下文字播放清單。"""
    hls_server.files["/hls/master"] = hls_server.files["/hls/master.m3u8"]
    out = tmp_path / "out"
    mgr = DownloadManager(Settings(download_dir=str(out), connections=4), store=tmp_path / "tasks.json")
    try:
        task = mgr.add(Task(url=hls_server.url("/hls/master?id=1"), kind=Kind.HTTP, title="第3集",
                            save_dir=str(out)))
        deadline = time.monotonic() + 120
        while task.status not in (Status.COMPLETED, Status.ERROR) and time.monotonic() < deadline:
            time.sleep(0.2)
        assert task.status == Status.COMPLETED, task.error
        assert task.kind == Kind.MEDIA
        result = out / task.filename
        assert task.filename == "第3集.mp4" and result.exists()
        probe = subprocess.run([ffmpeg_path(), "-hide_banner", "-i", str(result)], capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
        assert "Video: h264" in probe.stderr
    finally:
        mgr.shutdown()
