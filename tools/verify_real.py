"""真实抖音公开页面验证工具（需明确授权后使用）。

仅访问公开网页/接口，使用匿名会话 Cookie，不读取用户凭据，不绕过登录或验证码。
用法：
    .venv\\Scripts\\python.exe tools\\verify_real.py [web_rid 或完整直播 URL]
    .venv\\Scripts\\python.exe tools\\verify_real.py --api <web_rid>
    .venv\\Scripts\\python.exe tools\\verify_real.py --record <web_rid> [秒数]
录制模式使用临时隔离目录，验证后清理，不接触正式数据目录。
"""
from __future__ import annotations

import http.cookiejar
import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dlrecorder.infrastructure.http_client import UrllibHttpClient  # noqa: E402

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def inspect_render_data(text: str) -> None:
    script_ids = re.findall(r'<script[^>]*id="([^"]+)"', text)
    print(f"script ids: {script_ids}")
    hits: list[dict] = []

    def walk(node: object, path: str = "") -> None:
        if len(hits) >= 10:
            return
        if isinstance(node, dict):
            keys = set(node)
            if {"roomId", "room_id", "roomIdStr", "web_rid", "stream_url"} & keys:
                hits.append(
                    {
                        "path": path,
                        "keys": sorted(keys)[:40],
                        **{
                            k: node[k]
                            for k in (
                                "roomId",
                                "room_id",
                                "roomIdStr",
                                "web_rid",
                                "title",
                                "status",
                            )
                            if k in node
                        },
                    }
                )
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node[:20]):
                walk(value, f"{path}[{index}]")

    found = False
    for script_id in dict.fromkeys((*script_ids, "RENDER_DATA")):
        match = re.search(
            rf'<script[^>]*id="{re.escape(script_id)}"[^>]*>(.*?)</script>',
            text,
            re.DOTALL,
        )
        if not match:
            continue
        found = True
        raw = match.group(1)
        try:
            data = json.loads(urllib.parse.unquote(raw))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            print(f"script {script_id}: len={len(raw)} non-JSON={exc}; head={raw[:200]!r}")
            continue
        top = list(data) if isinstance(data, dict) else type(data).__name__
        print(f"script {script_id}: len={len(raw)} top={top}")
        walk(data, script_id)
    if not found:
        pos = text.find("RENDER_DATA")
        print(f"embedded data script not found; context: {text[pos:pos + 200]}")
    for hit in hits:
        print(json.dumps(hit, ensure_ascii=False)[:2000])


def probe(url: str) -> None:
    # 匿名 Cookie 会话（如 ttwid），不涉及用户凭据；第二次请求模拟浏览器回访。
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    client = UrllibHttpClient(opener)
    for attempt in (1, 2):
        resp = client.get(url, headers=dict(HEADERS), timeout=15.0)
        print(f"URL={url} attempt={attempt}")
        print(
            f"status={resp.status} body_len={len(resp.body)} "
            f"content_type={resp.content_type!r} error={resp.error_kind}/{resp.error_message}"
        )
        print(f"cookies: {[c.name for c in jar]}")
        text = resp.body.decode("utf-8", "replace")
        print("--- markers ---")
        target_id = urllib.parse.urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
        for marker in (
            "RENDER_DATA",
            "_ROUTER_DATA",
            "roomId",
            "web_rid",
            "stream_url",
            target_id,
            "captcha",
            "verify",
        ):
            pos = text.find(marker)
            print(f"{marker}: {pos}")
            if marker == target_id and pos >= 0:
                print(f"target context: {text[max(0, pos - 200):pos + 500]!r}")
        rids = re.findall(r"live\.douyin\.com/(\d{5,})", text)
        print(f"discovered web_rids: {sorted(set(rids))[:10]}")
        pace_chunks = re.findall(
            r'self\.__pace_f\.push\(\[1,\s*("(?:\\.|[^"\\])*")\s*\]\)', text
        )
        if pace_chunks:
            pace_data = "".join(json.loads(chunk) for chunk in pace_chunks)
            print(f"pace data: chunks={len(pace_chunks)} len={len(pace_data)}")
            for marker in (
                target_id,
                "webrid",
                "roomId",
                "room_id",
                "web_rid",
                "stream_url",
                "flv_pull_url",
                "hls_pull_url",
            ):
                positions = list(re.finditer(re.escape(marker), pace_data))
                print(f"pace {marker}: count={len(positions)}")
                for item in positions[:3]:
                    pos = item.start()
                    print(f"pace context: {pace_data[max(0, pos - 200):pos + 600]!r}")
            room_lines = [
                line
                for line in pace_data.splitlines()
                if '"roomId"' in line and '"web_rid"' in line
            ]
            print(f"flight room lines: {len(room_lines)}")
            for line in room_lines[:3]:
                payload = line.partition(":")[2]
                payload = re.sub(r"^T[0-9a-fA-F]+,", "", payload, count=1)
                try:
                    decoded, end = json.JSONDecoder().raw_decode(payload)
                    print(
                        f"flight room line decoded: {type(decoded).__name__} "
                        f"end={end} len={len(payload)}"
                    )
                except json.JSONDecodeError as exc:
                    pos = line.find(f'"web_rid":"{target_id}"')
                    print(
                        f"flight room line error={exc}; head={line[:100]!r}; "
                        f"error_context={payload[max(0, exc.pos - 100):exc.pos + 100]!r}; "
                        f"target={line[max(0, pos - 100):pos + 200]!r}; tail={line[-100:]!r}"
                    )
        inspect_render_data(text)
        from dlrecorder.douyin.parser import _extract_page_room

        room = _extract_page_room(resp.body, target_id)
        print(
            "parsed room:",
            None
            if room is None
            else {
                "keys": sorted(room),
                **{
                    key: room.get(key)
                    for key in ("roomId", "web_rid", "status", "title")
                },
            },
        )


def record_probe(web_rid: str, seconds: float = 5.0) -> None:
    """在临时隔离目录完成一次真实解析、短时录制与数据库收尾验证。"""
    from PySide6.QtCore import QCoreApplication

    from dlrecorder.application.recording_service import RecordingService
    from dlrecorder.domain.states import ParseResult
    from dlrecorder.douyin.parser import RealDouyinParser
    from dlrecorder.infrastructure.db import Database
    from dlrecorder.infrastructure.ffmpeg import find_local_ffmpeg

    qt_app = QCoreApplication.instance() or QCoreApplication([])
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    parser = RealDouyinParser(UrllibHttpClient(opener), timeout_s=20.0)
    detail = parser.fetch_and_parse_detail(web_rid)
    print(
        f"parse result={detail.result} status={detail.status} "
        f"stream_url={bool(detail.stream_url)}"
    )
    if detail.result != ParseResult.SUCCESS or detail.status != 2 or not detail.stream_url:
        raise RuntimeError("目标房间未确认开播或没有可用流地址，未开始录制")

    def wait_for(predicate, timeout: float, abort=None) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            qt_app.processEvents()
            if predicate():
                return True
            if abort is not None and abort():
                return False
            time.sleep(0.05)
        return False

    with tempfile.TemporaryDirectory(prefix="dlr-real-record-") as temp_dir:
        base = Path(temp_dir)
        db = Database(base / "app.db")
        room_id = db.add_room(web_rid, "真实房间短时验证")
        session_id = db.start_session(room_id)
        service = RecordingService(
            db,
            output_dir=base / "recordings",
            ffmpeg_path=find_local_ffmpeg(),
            min_free_mb=0,
            session_id_provider=lambda _room_id: session_id,
            poll_interval_s=0.1,
            stall_timeout_s=60,
            stop_grace_s=5,
            shutdown_timeout_s=30,
        )
        started: list[dict] = []
        finished: list[dict] = []
        errors: list[str] = []
        service.recording_started.connect(lambda _rid, snap: started.append(snap))
        service.recording_finished.connect(lambda _rid, snap: finished.append(snap))
        service.recording_error.connect(lambda _rid, message: errors.append(message))
        started_at = time.monotonic()
        try:
            service.start_recording(room_id, detail.stream_url)
            if not wait_for(
                lambda: bool(started), 45, abort=lambda: bool(errors) or bool(finished)
            ):
                raise RuntimeError(f"录制启动失败: errors={errors} finished={finished}")
            output_path = Path(started[0]["file_path"])

            def enough_media() -> bool:
                return (
                    output_path.exists()
                    and output_path.stat().st_size >= 65536
                    and time.monotonic() - started_at >= seconds
                )

            if not wait_for(
                enough_media, 60, abort=lambda: bool(errors) or bool(finished)
            ):
                size = output_path.stat().st_size if output_path.exists() else 0
                raise RuntimeError(
                    f"录制增长验证失败: size={size} errors={errors} finished={finished}"
                )
            service.stop_recording(room_id)
            if not wait_for(lambda: bool(finished), 30):
                raise RuntimeError(f"录制停止超时: errors={errors}")

            snapshot = finished[0]
            row = db.get_recording(snapshot["recording_id"])
            if snapshot["status"] != "completed" or snapshot["file_size"] <= 0:
                raise RuntimeError(f"录制未完成: {snapshot}")
            if row is None or row["status"] != "completed" or row["session_id"] != session_id:
                raise RuntimeError(f"录制历史收尾异常: {row}")

            codec_types: list[str] = []
            ffprobe = Path(find_local_ffmpeg()).with_name("ffprobe.exe")
            if ffprobe.exists():
                probed = subprocess.run(
                    [
                        str(ffprobe),
                        "-v",
                        "error",
                        "-show_entries",
                        "stream=codec_type",
                        "-of",
                        "json",
                        str(output_path),
                    ],
                    capture_output=True,
                    timeout=30,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if probed.returncode != 0:
                    raise RuntimeError(probed.stderr.decode("utf-8", "replace"))
                codec_types = [
                    stream["codec_type"]
                    for stream in json.loads(probed.stdout).get("streams", [])
                ]
            else:
                decoded = subprocess.run(
                    [
                        find_local_ffmpeg(),
                        "-hide_banner",
                        "-xerror",
                        "-i",
                        str(output_path),
                        "-t",
                        "0.2",
                        "-f",
                        "null",
                        "-",
                    ],
                    capture_output=True,
                    timeout=30,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                media_info = decoded.stderr.decode("utf-8", "replace")
                if decoded.returncode != 0:
                    raise RuntimeError(f"录制文件解码失败: {media_info[-2000:]}")
                codec_types = [
                    kind
                    for kind, marker in (("video", "Video:"), ("audio", "Audio:"))
                    if marker in media_info
                ]
            if "video" not in codec_types:
                raise RuntimeError(f"录制文件未检测到视频流: {codec_types}")

            print(
                json.dumps(
                    {
                        "result": "SUCCESS",
                        "elapsed_s": round(time.monotonic() - started_at, 2),
                        "file_name": output_path.name,
                        "file_size": snapshot["file_size"],
                        "db_status": row["status"],
                        "session_linked": True,
                        "codec_types": codec_types,
                    },
                    ensure_ascii=False,
                )
            )
        finally:
            report = service.shutdown()
            print(
                f"recording_shutdown thread_exited={report.thread_exited} "
                f"remaining_active={report.remaining_active}"
            )
            db.end_session(session_id)
            db.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--record":
        seconds = float(args[2]) if len(args) > 2 else 5.0
        if seconds <= 0:
            raise ValueError("录制秒数必须大于 0")
        record_probe(args[1], seconds)
    elif args and args[0] == "--api":
        from dlrecorder.douyin.parser import RealDouyinParser

        rid = args[1]
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        parser = RealDouyinParser(UrllibHttpClient(opener), timeout_s=15.0)
        for attempt in (1, 2):
            detail = parser.fetch_and_parse_detail(rid)
            print(
                f"attempt={attempt} result={detail.result} status={detail.status} "
                f"stream_url={bool(detail.stream_url)}"
            )
        raw = UrllibHttpClient(opener).get(
            parser._build_url(rid), headers=dict(parser.DEFAULT_HEADERS), timeout=15.0
        )
        print(f"raw status={raw.status} body={raw.body[:1000]!r}")
        from dlrecorder.douyin.parser import _extract_page_room

        room = _extract_page_room(raw.body, rid)
        if room is not None:
            text = raw.body.decode("utf-8", "replace")
            print(f"room keys: {sorted(room)}")
            print(
                f"room status={room.get('status')!r} "
                f"status_str={room.get('status_str')!r} "
                f"title={room.get('title')!r} roomId={room.get('roomId')!r} "
                f"web_rid={room.get('web_rid')!r}"
            )
            print(
                "web_stream_url:",
                json.dumps(room.get("web_stream_url"), ensure_ascii=False)[:500],
            )
            for marker in ("直播已结束", "未开播", "暂未开播", "回放", "live-status"):
                print(f"page {marker}: {text.find(marker)}")
    else:
        target = args[0] if args else ""
        if target.startswith("http://") or target.startswith("https://"):
            probe(target)
        else:
            probe(f"https://live.douyin.com/{target}" if target else "https://live.douyin.com/")
