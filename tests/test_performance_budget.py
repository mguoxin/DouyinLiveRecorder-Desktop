"""性能预算实测（目标机本地运行，数字会打印到测试输出）。

预算来源：docs/audit/V0.5.0-ARCHITECTURE.md §9。
"""
import statistics
import time
from pathlib import Path

from dlrecorder.application.recording_metrics import SegmentMetrics
from dlrecorder.domain.recording_query import RecordingQuery
from dlrecorder.infrastructure.db import Database


def test_metrics_sampling_budget_for_50_rooms():
    rooms = 50
    ticks = 300
    metrics = [SegmentMetrics(now=0.0) for _ in range(rooms)]
    durations_ms: list[float] = []
    for tick in range(ticks):
        now = float(tick)
        started = time.perf_counter()
        for index, item in enumerate(metrics):
            item.observe(now, tick * 1024 * (index + 1))
            item.snapshot(
                now,
                room_id=index,
                status="recording",
                session_bytes=0,
                session_segments=0,
            )
        durations_ms.append((time.perf_counter() - started) * 1000)

    ordered = sorted(durations_ms)
    mean_ms = statistics.fmean(durations_ms)
    p95_ms = ordered[int(len(ordered) * 0.95) - 1]
    print(
        f"\n[perf] metrics 50 rooms x {ticks} ticks: "
        f"mean={mean_ms:.3f} ms p95={p95_ms:.3f} ms max={max(ordered):.3f} ms"
    )
    assert mean_ms < 10.0
    assert p95_ms < 20.0
    assert all(len(item.trend()) <= 120 for item in metrics)


def test_search_budget_with_ten_thousand_recordings(tmp_path: Path):
    db = Database(tmp_path / "data" / "app.db")
    try:
        rows = [
            (
                None,
                None,
                f"2025-01-{(index % 28) + 1:02d}T03:04:05Z",
                "2025-01-02T04:04:05Z",
                f"C:/recordings/主播{index % 500}/clip_{index}.ts",
                1024 + index,
                "completed",
                f"rid-{index % 500}",
                f"主播{index % 500}",
                f"anchor-{index % 500}",
                "captured",
                "none",
            )
            for index in range(10_000)
        ]
        with db._lock:
            conn = db._get_conn()
            conn.executemany(
                "INSERT INTO recordings(room_id, session_id, started_at, ended_at,"
                " file_path, file_size, status, web_rid_snapshot,"
                " display_name_snapshot, anchor_id_snapshot, snapshot_source,"
                " archive_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )

        query = RecordingQuery(keyword="主播499", limit=500)
        timings = []
        for _ in range(5):
            started = time.perf_counter()
            found = db.search_recordings(query)
            timings.append((time.perf_counter() - started) * 1000)
        mean_ms = statistics.fmean(timings)
        print(
            f"\n[perf] search 10k rows: mean={mean_ms:.2f} ms "
            f"max={max(timings):.2f} ms matched={db.count_recordings(query)}"
        )
        assert found
        assert mean_ms < 300.0

        started = time.perf_counter()
        stats = db.recording_storage_stats()
        stats_ms = (time.perf_counter() - started) * 1000
        print(f"[perf] storage stats 10k rows: {stats_ms:.2f} ms")
        assert stats["total_count"] == 10_000
        assert stats_ms < 1000.0
    finally:
        db.close()
