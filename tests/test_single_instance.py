"""单实例锁测试：全部使用真实独立进程与独立临时数据库。

验证命名互斥量的取得、拒绝、释放，以及第二实例不初始化数据库或监控服务。
"""
import ctypes
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from dlrecorder.__main__ import EXIT_ALREADY_RUNNING

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
PYTHON = sys.executable
BUSY_EXIT = 2

HOLDER_SCRIPT = """
import sys
import time
from pathlib import Path

from dlrecorder.application.app import Application
from dlrecorder.infrastructure.single_instance import SingleInstanceGuard

mutex_name, base_dir, ready_path, stop_path = sys.argv[1:5]
guard = SingleInstanceGuard(mutex_name)
if not guard.acquire():
    print("LOCK_BUSY", flush=True)
    sys.exit(2)

app = Application(base_dir=Path(base_dir))
room_id = app.service.add_room("instance-room", "单实例测试")
app.service.start_monitor(room_id)
Path(ready_path).write_text("ready", encoding="utf-8")
print("READY", flush=True)

while not Path(stop_path).exists():
    app.qt_app.processEvents()
    time.sleep(0.02)

app.shutdown()
guard.release()
print("EXITED", flush=True)
"""

SECOND_INSTANCE_SCRIPT = """
import sys

from dlrecorder.__main__ import main

code = main(
    notify=lambda message: print("NOTICE:" + message, flush=True),
    mutex_name=sys.argv[1],
)
print("MAIN_EXIT", code, flush=True)
sys.exit(code)
"""

PROBE_SCRIPT = """
import sys

from dlrecorder.infrastructure.single_instance import SingleInstanceGuard

guard = SingleInstanceGuard(sys.argv[1])
acquired = guard.acquire()
print("ACQUIRED" if acquired else "BUSY", flush=True)
guard.release()
sys.exit(0 if acquired else 2)
"""

# 模拟新入口语义：取得互斥量后绝不显式释放，由非守护线程维持进程存活。
# 用于验证“进程存活期间锁必须被持有、进程终止后由内核释放”。
LINGER_SCRIPT = """
import sys
import threading
import time
from pathlib import Path

from dlrecorder.infrastructure.single_instance import SingleInstanceGuard

mutex_name, ready_path, finish_path = sys.argv[1:4]
guard = SingleInstanceGuard(mutex_name)
if not guard.acquire():
    print("LOCK_BUSY", flush=True)
    sys.exit(2)


def lingering_worker():
    while not Path(finish_path).exists():
        time.sleep(0.02)


thread = threading.Thread(target=lingering_worker)
thread.start()
Path(ready_path).write_text("ready", encoding="utf-8")
print("READY", flush=True)
# 刻意不调用 guard.release()：句柄保留至进程终止。
"""

ERROR_ACCESS_DENIED = 5


class _NullCreateMutex:
    """返回 NULL 并设置 ERROR_ACCESS_DENIED 的 CreateMutexW 替身。"""

    argtypes = None
    restype = None

    def __call__(self, *args):
        ctypes.set_last_error(ERROR_ACCESS_DENIED)
        return None


class _UnexpectedCloseHandle:
    """CreateMutexW 失败路径不应关闭任何句柄。"""

    argtypes = None
    restype = None

    def __call__(self, handle):
        raise AssertionError("CreateMutexW 失败时不应调用 CloseHandle")


class _FakeKernel32:
    """仅存活于测试进程的 kernel32 替身，不创建真实系统互斥量。"""

    def __init__(self) -> None:
        self.CreateMutexW = _NullCreateMutex()
        self.CloseHandle = _UnexpectedCloseHandle()


def _env() -> dict:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(SRC_DIR) + (os.pathsep + existing if existing else "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["QT_QPA_PLATFORM"] = "offscreen"
    return env


def _mutex_name() -> str:
    return f"Local\\DlrecorderTest-{uuid.uuid4().hex}"


def _wait_until(predicate, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _read_sessions(db_path: Path):
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        return [
            tuple(row)
            for row in conn.execute(
                "SELECT id, status, ended_at, room_id "
                "FROM monitor_sessions ORDER BY id"
            )
        ]
    finally:
        conn.close()


def _write_script(tmp_path: Path, name: str, code: str) -> Path:
    path = tmp_path / name
    path.write_text(code, encoding="utf-8")
    return path


class _Holder:
    def __init__(self, proc: subprocess.Popen, stop_path: Path) -> None:
        self.proc = proc
        self.stop_path = stop_path

    def stop(self) -> int:
        self.stop_path.write_text("stop", encoding="utf-8")
        return self.proc.wait(timeout=60)

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=10)

    def terminate(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - 防御性清理
            pass


def _start_holder(tmp_path: Path, mutex_name: str) -> _Holder:
    script = _write_script(tmp_path, "holder_instance.py", HOLDER_SCRIPT)
    ready = tmp_path / "holder_ready.flag"
    stop = tmp_path / "holder_stop.flag"
    proc = subprocess.Popen(
        [PYTHON, str(script), mutex_name, str(tmp_path), str(ready), str(stop)],
        cwd=str(tmp_path),
        env=_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    holder = _Holder(proc, stop)
    if not _wait_until(ready.exists):
        holder.terminate()
        assert False, f"第一实例未就绪: {proc.stderr.read() if proc.stderr else ''}"
    return holder


def _run_second_instance(tmp_path: Path, mutex_name: str) -> subprocess.CompletedProcess:
    script = _write_script(tmp_path, "second_instance.py", SECOND_INSTANCE_SCRIPT)
    return subprocess.run(
        [PYTHON, str(script), mutex_name],
        cwd=str(tmp_path),
        env=_env(),
        capture_output=True,
        text=True,
        timeout=120,
    )


def _run_probe(tmp_path: Path, mutex_name: str) -> subprocess.CompletedProcess:
    script = _write_script(tmp_path, "probe_instance.py", PROBE_SCRIPT)
    return subprocess.run(
        [PYTHON, str(script), mutex_name],
        cwd=str(tmp_path),
        env=_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )


class _LingerHolder:
    def __init__(self, proc: subprocess.Popen, finish_path: Path) -> None:
        self.proc = proc
        self.finish_path = finish_path

    def finish(self) -> int:
        self.finish_path.write_text("finish", encoding="utf-8")
        return self.proc.wait(timeout=60)

    def terminate(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - 防御性清理
            pass


def _start_linger(tmp_path: Path, mutex_name: str) -> _LingerHolder:
    script = _write_script(tmp_path, "linger_instance.py", LINGER_SCRIPT)
    ready = tmp_path / "linger_ready.flag"
    finish = tmp_path / "linger_finish.flag"
    proc = subprocess.Popen(
        [PYTHON, str(script), mutex_name, str(ready), str(finish)],
        cwd=str(tmp_path),
        env=_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    holder = _LingerHolder(proc, finish)
    if not _wait_until(ready.exists):
        holder.terminate()
        assert False, f"linger 实例未就绪: {proc.stderr.read() if proc.stderr else ''}"
    return holder


def _probe_until_acquired(tmp_path: Path, mutex_name: str, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = _run_probe(tmp_path, mutex_name)
        if result.returncode == 0 and "ACQUIRED" in result.stdout:
            return True
        time.sleep(0.1)
    return False


def test_second_instance_rejected_without_touching_first(tmp_path):
    """第二实例取得锁失败，且不初始化数据库、不修改第一实例的 running Session。"""
    mutex_name = _mutex_name()
    holder = _start_holder(tmp_path, mutex_name)
    db_path = tmp_path / "data" / "app.db"
    try:
        assert _wait_until(
            lambda: db_path.exists() and len(_read_sessions(db_path)) == 1
        ), "第一实例应创建唯一的 running Session"
        before = _read_sessions(db_path)
        assert before[0][1] == "running"

        db_mtime_before = db_path.stat().st_mtime_ns
        second = _run_second_instance(tmp_path, mutex_name)
        db_mtime_after = db_path.stat().st_mtime_ns
        assert second.returncode == EXIT_ALREADY_RUNNING
        assert "NOTICE:" in second.stdout and "已在运行" in second.stdout
        assert f"MAIN_EXIT {EXIT_ALREADY_RUNNING}" in second.stdout

        # 第二实例未初始化数据库（mtime 未变），未创建新 Session，也未把 running 改为 interrupted
        assert db_mtime_after == db_mtime_before, "第二实例不得写入数据库文件"
        assert _read_sessions(db_path) == before
        assert holder.proc.poll() is None, "第一实例应仍在运行"
    finally:
        holder.terminate()


def test_lock_released_after_normal_exit(tmp_path):
    """第一实例正常退出后，锁可被再次取得。"""
    mutex_name = _mutex_name()
    holder = _start_holder(tmp_path, mutex_name)
    try:
        busy = _run_probe(tmp_path, mutex_name)
        assert busy.returncode == BUSY_EXIT and "BUSY" in busy.stdout
    finally:
        exit_code = holder.stop()
    assert exit_code == 0, "第一实例应正常退出"
    assert _probe_until_acquired(tmp_path, mutex_name)


def test_lock_released_after_abnormal_termination(tmp_path):
    """第一实例被强制终止后，操作系统释放内核互斥量，锁可再次取得。"""
    mutex_name = _mutex_name()
    holder = _start_holder(tmp_path, mutex_name)
    try:
        assert _run_probe(tmp_path, mutex_name).returncode == BUSY_EXIT
    finally:
        holder.kill()
    assert _probe_until_acquired(tmp_path, mutex_name)


def test_repeated_start_attempts_never_create_second_instance(tmp_path):
    """第一实例存活期间多次重复启动均被拒绝，不会产生多个有效实例。"""
    mutex_name = _mutex_name()
    holder = _start_holder(tmp_path, mutex_name)
    try:
        for _ in range(3):
            result = _run_probe(tmp_path, mutex_name)
            assert result.returncode == BUSY_EXIT
            assert "BUSY" in result.stdout
        assert holder.proc.poll() is None
    finally:
        holder.stop()
    assert _probe_until_acquired(tmp_path, mutex_name)


def test_t16_mutex_held_while_old_process_still_alive(tmp_path):
    """T16：旧进程仍存活时 Mutex 仍被持有，第二实例无法启动（真实子进程验证）。"""
    mutex_name = _mutex_name()
    holder = _start_linger(tmp_path, mutex_name)
    try:
        probe = _run_probe(tmp_path, mutex_name)
        assert probe.returncode == BUSY_EXIT and "BUSY" in probe.stdout

        second = _run_second_instance(tmp_path, mutex_name)
        assert second.returncode == EXIT_ALREADY_RUNNING
        assert "NOTICE:" in second.stdout and "已在运行" in second.stdout
        assert holder.proc.poll() is None, "旧进程应仍存活并继续持有互斥量"
    finally:
        holder.terminate()


def test_t17_mutex_released_by_kernel_after_process_exit(tmp_path):
    """T17：旧进程终止（未显式释放）后，内核释放 Mutex，后续进程可以启动。"""
    mutex_name = _mutex_name()
    holder = _start_linger(tmp_path, mutex_name)
    try:
        assert _run_probe(tmp_path, mutex_name).returncode == BUSY_EXIT
    finally:
        exit_code = holder.finish()
    assert exit_code == 0, "linger 进程应自然结束"
    assert _probe_until_acquired(tmp_path, mutex_name)


def test_mutex_creation_failure_is_not_treated_as_allowed(monkeypatch):
    """互斥量创建失败时必须拒绝启动，并返回独立的错误退出码。"""
    from dlrecorder import __main__ as entry
    from dlrecorder.infrastructure import single_instance

    def broken_kernel32():
        raise single_instance.SingleInstanceError("CreateMutexW failed")

    monkeypatch.setattr(single_instance, "_load_kernel32", broken_kernel32)
    notices: list[str] = []

    code = entry.main(
        notify=notices.append,
        mutex_name="Local\\DlrecorderTest-error",
    )

    assert code == entry.EXIT_INSTANCE_LOCK_ERROR
    assert notices and "单实例检测失败" in notices[0]


def test_create_mutex_null_is_fail_closed(monkeypatch):
    """CreateMutexW 返回 NULL 时抛出 SingleInstanceError，入口 fail-closed 且不创建 Application。"""
    from dlrecorder import __main__ as entry
    from dlrecorder.infrastructure import single_instance

    fake = _FakeKernel32()
    monkeypatch.setattr(single_instance, "_load_kernel32", lambda: fake)

    with pytest.raises(single_instance.SingleInstanceError) as excinfo:
        single_instance.SingleInstanceGuard("Local\\DlrecorderTest-null").acquire()
    assert "WinError=5" in str(excinfo.value)

    created: list[object] = []
    monkeypatch.setattr(entry, "Application", lambda *args, **kwargs: created.append((args, kwargs)))

    notices: list[str] = []
    code = entry.main(
        notify=notices.append,
        mutex_name="Local\\DlrecorderTest-null-entry",
    )

    assert code == entry.EXIT_INSTANCE_LOCK_ERROR
    assert notices and "单实例检测失败" in notices[0]
    assert "WinError=5" in notices[0]
    # 取锁失败时不得创建 Application，数据库、遗留 Session 与监控服务均不初始化
    assert created == []


def test_startup_failure_is_reported_to_user(monkeypatch):
    """数据库/权限等启动失败必须返回独立退出码并给出可读提示。"""
    from dlrecorder import __main__ as entry

    def broken_application(*args, **kwargs):
        raise RuntimeError("数据库文件损坏")

    monkeypatch.setattr(entry, "Application", broken_application)
    notices: list[str] = []

    code = entry.main(
        notify=notices.append,
        mutex_name="Local\\DlrecorderTest-startup-failure",
    )

    assert code == entry.EXIT_STARTUP_FAILED
    assert notices and "启动失败" in notices[0]
    assert "数据库文件损坏" in notices[0]

