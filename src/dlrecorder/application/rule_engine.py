"""自动化规则引擎（P1）。

职责：
- 周期性评估时间窗规则（定时监控/定时录制）与磁盘预警；
- 响应录制完成事件执行录制后处理（标签/收藏）；
- 为录制完成事件提供按房间的自动归档决策。

安全约束：
- 引擎只停止由自己启动的监控/录制，不打断用户手动开始的任务。
- 磁盘预警默认只通知，不主动删除或停止录制。
- 规则执行失败写入 ``rule_runs`` 并通知，不静默吞掉。
- 上次异常退出遗留的 ``running`` 记录在启动时标记为失败，保证历史可信。
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal

from ..domain.automation_rules import (
    AutomationRule,
    RuleConfigError,
    RULE_TYPE_LABELS,
)
from ..infrastructure.db import Database

logger = logging.getLogger(__name__)

DEFAULT_TICK_INTERVAL_S = 60.0
_MAX_ERROR_LEN = 500


@dataclass
class RuleActions:
    """引擎与业务层之间的显式命令集合（由应用装配注入）。"""

    is_monitoring: Callable[[int], bool]
    start_monitor: Callable[[int], bool]
    stop_monitor: Callable[[int], bool]
    is_recording: Callable[[int], bool]
    start_recording: Callable[[int], bool]
    stop_recording: Callable[[int], bool]
    has_stream: Callable[[int], bool]
    notify: Callable[[str, str], None]
    free_space_mb: Callable[[], Optional[float]]


class RuleEngine(QObject):
    """后台规则评估器；UI 只读取执行历史与规则表。"""

    rule_run_recorded = Signal(int, dict)

    def __init__(
        self,
        db: Database,
        actions: RuleActions,
        *,
        tick_interval_s: float = DEFAULT_TICK_INTERVAL_S,
        clock: Optional[Callable[[], datetime]] = None,
        auto_start: bool = False,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._actions = actions
        self._interval = max(1.0, float(tick_interval_s))
        self._clock = clock or datetime.now
        self._condition = threading.Condition()
        self._events: deque[tuple[int, int, str]] = deque()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # 引擎启动的监控/录制；只有这些才会被自动停止。
        self._managed: set[tuple[str, int]] = set()
        self._disk_latched: set[int] = set()
        self._invalid_reported: set[tuple[int, str]] = set()
        self._processed_finished: set[int] = set()
        if auto_start:
            self.start()

    # ---------- 生命周期 ----------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self.recover_stale_runs()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="dlr-rule-engine", daemon=True
        )
        self._thread.start()

    def recover_stale_runs(self) -> int:
        count = self._db.mark_stale_rule_runs("引擎异常退出，任务未完成")
        if count:
            logger.warning("规则引擎恢复：%s 条 running 记录标记为失败", count)
        return count

    def shutdown(self, timeout: float = 5.0) -> bool:
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, timeout))
        exited = thread is None or not thread.is_alive()
        if not exited:
            logger.critical("规则引擎线程未在 %.1fs 内退出", timeout)
        return exited

    # ---------- 事件入口 ----------
    def on_recording_finished(
        self, recording_id: int, room_id: int, status: str
    ) -> None:
        with self._condition:
            self._events.append((int(recording_id), int(room_id), status))
            self._condition.notify_all()

    def archive_decision(self, room_id: int) -> Optional[bool]:
        """返回该房间的归档决策；无匹配规则时返回 None（沿用全局设置）。"""
        rules = self._enabled_rules("archive")
        matching = [
            rule for rule in rules if rule.room_id in (None, int(room_id))
        ]
        if not matching:
            return None
        # 房间级规则优先于全局规则，其次按优先级与创建顺序。
        matching.sort(key=lambda rule: (rule.room_id is None, rule.priority, rule.id))
        try:
            return matching[0].archive_enabled()
        except RuleConfigError as exc:
            self._report_invalid(matching[0], str(exc))
            return None

    # ---------- 工作线程 ----------
    def _run(self) -> None:
        while True:
            with self._condition:
                if not self._events and not self._stop.is_set():
                    self._condition.wait(timeout=self._interval)
                if self._stop.is_set() and not self._events:
                    return
                event = self._events.popleft() if self._events else None
            try:
                if event is not None:
                    self._handle_finished_event(*event)
                else:
                    self.tick_once()
            except Exception:  # noqa: BLE001 - 单轮失败不得终止引擎
                logger.exception("规则引擎执行异常")

    # ---------- 手动触发（测试与诊断） ----------
    def tick_once(self, now: Optional[datetime] = None) -> int:
        moment = now or self._clock()
        rules = self._load_rules()
        performed = self._apply_schedule_rules(rules, moment)
        performed += self._apply_disk_guard(rules)
        return performed

    # ---------- 规则加载 ----------
    def _load_rules(self) -> list[AutomationRule]:
        parsed: list[AutomationRule] = []
        for row in self._db.list_automation_rules(enabled_only=True):
            try:
                parsed.append(AutomationRule.from_row(row))
            except (RuleConfigError, KeyError, TypeError) as exc:
                logger.warning("忽略非法规则 #%s: %s", row.get("id"), exc)
        return parsed

    def _enabled_rules(self, rule_type: str) -> list[AutomationRule]:
        return [rule for rule in self._load_rules() if rule.rule_type == rule_type]

    # ---------- 时间窗规则 ----------
    def _apply_schedule_rules(
        self, rules: list[AutomationRule], moment: datetime
    ) -> int:
        performed = 0
        for rule_type, kind in (
            ("monitor_schedule", "monitor"),
            ("recording_schedule", "recording"),
        ):
            grouped: dict[int, list[AutomationRule]] = {}
            for rule in rules:
                if rule.rule_type != rule_type or rule.room_id is None:
                    continue
                grouped.setdefault(rule.room_id, []).append(rule)
            for room_id, candidates in grouped.items():
                # 列表已按 priority、id 升序，数字小者优先。
                winner = candidates[0]
                try:
                    desired = winner.schedule().contains(moment)
                except RuleConfigError as exc:
                    self._report_invalid(winner, str(exc))
                    continue
                performed += self._transition(winner, kind, room_id, desired)
        return performed

    def _transition(
        self,
        rule: AutomationRule,
        kind: str,
        room_id: int,
        desired: bool,
    ) -> int:
        key = (kind, room_id)
        try:
            if kind == "monitor":
                actual = self._actions.is_monitoring(room_id)
                if desired and not actual:
                    ok = self._actions.start_monitor(room_id)
                    if ok:
                        self._managed.add(key)
                        self._record_run(rule, room_id, "succeeded", "进入定时监控时段")
                        return 1
                    self._record_run(rule, room_id, "failed", "启动监控失败")
                    return 0
                if not desired and actual and key in self._managed:
                    ok = self._actions.stop_monitor(room_id)
                    self._managed.discard(key)
                    self._record_run(
                        rule, room_id,
                        "succeeded" if ok else "failed",
                        "离开定时监控时段",
                    )
                    return 1
            else:
                actual = self._actions.is_recording(room_id)
                if desired and not actual:
                    if not self._actions.has_stream(room_id):
                        return 0
                    ok = self._actions.start_recording(room_id)
                    if ok:
                        self._managed.add(key)
                        self._record_run(rule, room_id, "succeeded", "进入定时录制时段")
                        return 1
                    self._record_run(rule, room_id, "failed", "启动录制失败")
                    return 0
                if not desired and actual and key in self._managed:
                    ok = self._actions.stop_recording(room_id)
                    self._managed.discard(key)
                    self._record_run(
                        rule, room_id,
                        "succeeded" if ok else "failed",
                        "离开定时录制时段",
                    )
                    return 1
        except Exception as exc:  # noqa: BLE001 - 业务动作异常必须留痕
            logger.exception("规则 #%s 执行失败", rule.id)
            self._record_run(rule, room_id, "failed", f"执行异常: {exc}")
            self._notify(
                "自动化规则执行失败",
                f"{rule.name}：{exc}"[:300],
            )
        return 0

    # ---------- 磁盘预警 ----------
    def _apply_disk_guard(self, rules: list[AutomationRule]) -> int:
        guards = [rule for rule in rules if rule.rule_type == "disk_guard"]
        if not guards:
            return 0
        try:
            free_mb = self._actions.free_space_mb()
        except Exception:  # noqa: BLE001 - 读取磁盘失败只跳过本轮
            logger.warning("读取磁盘剩余空间失败，跳过磁盘预警", exc_info=True)
            return 0
        if free_mb is None:
            return 0
        performed = 0
        for rule in guards:
            try:
                threshold = rule.min_free_mb()
            except RuleConfigError as exc:
                self._report_invalid(rule, str(exc))
                continue
            below = free_mb < threshold
            room_id = rule.room_id or 0
            if below and rule.id not in self._disk_latched:
                self._disk_latched.add(rule.id)
                message = (
                    f"剩余空间 {free_mb:.0f} MB 低于阈值 {threshold:.0f} MB"
                )
                self._record_run(rule, room_id, "succeeded", message)
                self._notify("磁盘空间预警", message)
                performed += 1
            elif not below and rule.id in self._disk_latched:
                self._disk_latched.discard(rule.id)
                message = f"剩余空间已恢复至 {free_mb:.0f} MB"
                self._record_run(rule, room_id, "succeeded", message)
                self._notify("磁盘空间恢复", message)
                performed += 1
        return performed

    # ---------- 录制后处理 ----------
    def _handle_finished_event(
        self, recording_id: int, room_id: int, status: str
    ) -> None:
        if status != "completed":
            return
        if recording_id in self._processed_finished:
            return
        rules = [
            rule
            for rule in self._enabled_rules("post_process")
            if rule.room_id in (None, room_id)
        ]
        if not rules:
            self._processed_finished.add(recording_id)
            return
        recording = self._db.get_recording(recording_id)
        if recording is None:
            return
        for rule in rules:
            try:
                tags, favorite = rule.post_process_values()
            except RuleConfigError as exc:
                self._report_invalid(rule, str(exc))
                continue
            run_id = self._db.create_rule_run(rule.id, room_id, status="running")
            try:
                if tags:
                    self._assign_tags(recording_id, tags)
                if favorite:
                    self._db.set_recording_favorite(recording_id, True)
                message = f"应用 {len(tags)} 个标签" if tags else "后处理完成"
                self._db.finish_rule_run(run_id, "succeeded", message)
                self._emit_run(rule, run_id, "succeeded", message)
            except Exception as exc:  # noqa: BLE001
                logger.exception("录制后处理失败 recording_id=%s", recording_id)
                self._db.finish_rule_run(run_id, "failed", f"执行异常: {exc}")
                self._emit_run(rule, run_id, "failed", f"执行异常: {exc}")
                self._notify("录制后处理失败", f"{rule.name}：{exc}"[:300])
        self._processed_finished.add(recording_id)

    def _assign_tags(self, recording_id: int, tag_names: list[str]) -> None:
        existing = {
            int(tag["id"]) for tag in self._db.get_recording_tags(recording_id)
        }
        for name in tag_names:
            existing.add(self._db.create_tag(name))
        self._db.set_recording_tags(recording_id, sorted(existing))

    # ---------- 内部辅助 ----------
    def _record_run(
        self,
        rule: AutomationRule,
        room_id: int,
        status: str,
        message: str,
    ) -> None:
        run_id = self._db.create_rule_run(rule.id, room_id or None, status="running")
        self._db.finish_rule_run(run_id, status, message[:_MAX_ERROR_LEN])
        self._emit_run(rule, run_id, status, message)

    def _emit_run(
        self, rule: AutomationRule, run_id: int, status: str, message: str
    ) -> None:
        if status == "failed":
            logger.warning("规则执行失败 #%s %s: %s", rule.id, rule.name, message)
        else:
            logger.info("规则执行 #%s %s: %s", rule.id, rule.name, message)
        payload = {
            "run_id": run_id,
            "rule_id": rule.id,
            "rule_name": rule.name,
            "rule_type": rule.rule_type,
            "type_label": RULE_TYPE_LABELS.get(rule.rule_type, rule.rule_type),
            "status": status,
            "message": message[:_MAX_ERROR_LEN],
        }
        try:
            self.rule_run_recorded.emit(rule.id, payload)
        except RuntimeError:
            logger.debug("规则运行信号发送失败 rule_id=%s", rule.id)

    def _report_invalid(self, rule: AutomationRule, message: str) -> None:
        key = (rule.id, message)
        if key in self._invalid_reported:
            return
        self._invalid_reported.add(key)
        logger.warning("规则配置非法 #%s: %s", rule.id, message)
        run_id = self._db.create_rule_run(rule.id, rule.room_id, status="running")
        self._db.finish_rule_run(run_id, "failed", f"配置非法: {message}")
        self._emit_run(rule, run_id, "failed", f"配置非法: {message}")
        self._notify("自动化规则配置非法", f"{rule.name}：{message}"[:300])

    def _notify(self, title: str, message: str) -> None:
        try:
            self._actions.notify(title, message)
        except Exception:  # noqa: BLE001 - 通知失败不得影响规则执行
            logger.exception("规则通知发送失败")
