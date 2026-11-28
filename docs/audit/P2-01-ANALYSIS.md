# P2-01 专项分析 — 会话结束事件与分段结束事件混淆

> 基线：提交 `c8ec7c2` / 标签 `v0.4.0`。
> 本文件只做分析与方案设计，**未修改任何源码**。修复需另行授权（L3 实施阶段）。

---

## 1. 原始问题定义（先核实，不自行推断）

来源：`docs/AUDIT_0.3.1_RECORDING_STATUS.md` 第 11.1 节（L131-138），原文要点：

- **现象**：会话收尾（直播结束 / 续录耗尽 / 等待续录期间手动停止）复用了最后一个分段的 `file_path`，但 `status`、`file_size` 为会话语义（`completed` / `0`），GUI 据此输出「录制结束 status=completed size=0」。日志与历史表（读数据库）不一致。
- **位置**：`recording_service._end_without_segment`（事件构造）、`main_window._on_recording_finished`（消费）。
- **影响**：仅运行日志与状态栏文案；数据库 `recordings` 内容、录制文件与进程清理均正确。
- **状态**：未修复；候选版 2（`f168880`）沿用同一录制核心，同样存在。

审计确认：以上定义与当前 `c8ec7c2` 代码完全一致，**没有发现被夸大的部分，也没有遗漏的触发面**。

---

## 2. 代码路径核实

### 2.1 事件构造（会话语义，却复用分段字段）

`src/dlrecorder/application/recording_service.py:819-841`

```python
def _end_without_segment(self, active, message, *, failed):
    """结束没有打开分段的录制会话（重试等待中放弃或手动停止）。"""
    if not self._drop_active(active):
        return
    status = RecordingStatus.FAILED if failed else RecordingStatus.COMPLETED
    ...
    self.recording_finished.emit(
        active.room_id,
        {
            "recording_id": None,
            "room_id": active.room_id,
            "file_path": str(active.file_path),   # ← 指向从未创建的“下一段”路径
            "status": status.value,
            "file_size": 0,                       # ← 会话语义，与 status 组合后被展示为 completed size=0
            "error": message if failed else "",
        },
    )
```

### 2.2 正常分段收尾（同一事件名，字段语义不同）

`recording_service.py:905-915` 发出的同一条 `recording_finished`，此时 `recording_id`、`file_size`、`file_path` 均为真实分段数据。

**同一信号名承载两种语义**，消费端无法区分，这是问题本质。

### 2.3 消费端

`src/dlrecorder/presentation/main_window.py:505-520`

```python
def _on_recording_finished(self, room_id, snapshot):
    ...
    size = snapshot.get("file_size", 0)
    ...
```

GUI 直接把 `file_size` 拼进状态栏与日志文案，没有读取 `recording_id is None` 这一可判别信号。

另有一个**非渲染**消费点：`application/app.py:191 → :239`，其 `_on_recording_finished` 只执行 `self._auto_pending.discard(room_id)`，不读取任何字段，因此不受本次语义修正影响。

### 2.4 数据库侧（正确，不参与问题）

`infrastructure/db.py:789-808` 的 `finish_recording` 只对 `status='recording'` 的行生效，幂等；`_end_without_segment` 路径本就没有分段记录，因此**不会写入错误数据**。历史表与录制文件不受影响，这与原始登记一致。

---

## 3. 触发条件（三类，均已可在代码中定位）

| 触发场景 | 代码入口 | 事件结果 |
| --- | --- | --- |
| 直播自然结束、无活动分段时直接收尾 | `_poll_reconnect` 判定 offline/放弃 → `_end_without_segment(failed=False)` | `status=completed`, `file_size=0`, `file_path` 指向未创建文件 |
| 自动续录重试耗尽 | `_begin_reconnect` 预算用尽 → `_end_without_segment(failed=True)` | `status=failed`, `file_size=0`，另有 `recording_error` |
| 等待续录期间用户手动停止 | `_handle_stop`（`recording_service.py:469-487`）→ `_end_without_segment(failed=True, message="手动停止")` | 同上 |

第一类是用户最容易遇到的（直播自然结束），也是「completed size=0」文案的来源。

---

## 4. 根本原因

1. **事件契约缺少「作用域」维度**：`recording_finished` 同时承担「一个分段结束」与「一个会话结束」两种事实，二者字段集合相同但语义不同。
2. **缺少可判别标识**：消费端只能看到 `recording_id=None`，但该字段从未被文档化为「会话级事件」的标志，且 `file_path`/`file_size` 仍带着值，诱导消费端按分段语义渲染。
3. **文案生成在表现层**：`completed` + `0` 的组合本身没有错（会话确实完成、没有产出文件），错在 UI 把会话语义当成分段语义展示。

**不是**状态机错误，**不是**数据库写入错误，**不是**进程清理错误。

---

## 5. 用户实际影响

| 影响 | 程度 |
| --- | --- |
| 日志出现「录制结束 status=completed size=0」 | 误导排障，用户可能以为录制失败或文件丢失 |
| 状态栏/历史页文案与历史表（size=8372580 等真实值）不一致 | 困惑，产生「文件是不是没保存」的疑问 |
| 录制文件、数据库、进程清理 | **无影响** |
| 自动化测试 | 无影响（现有断言不检查该文案） |

---

## 6. 修复风险

| 风险 | 说明 | 缓解 |
| --- | --- | --- |
| 事件载荷变更影响其他消费端 | 该信号有 2 个消费点：`app.py:191 → :239`（仅 `_auto_pending.discard`，不读任何字段）与 `main_window.py:197 → :505`（唯一渲染字段处） | 修复前用 `grep -rn "recording_finished" src tests` 再确认一次；只新增键、不删旧键 |
| 现有测试断言依赖字段集合 | `tests/test_app.py:478/536/595/659`、`tests/test_disk_protection.py:85` 等连接了该信号并可能断言载荷 | 新增键即可保持通过；实施前逐一核对这些用例读取了哪些字段 |
| GUI 文案回归 | 需要同时覆盖「分段结束」与「会话结束」两条渲染分支 | 新增 2 条 GUI 层断言或 1 条 handler 单测 |
| 录制状态机被误改 | 若顺手改 `_clean_exit_outcome` 或续录预算，会把低风险修复扩大为高风险改造 | 明确边界：本次**只碰事件构造与消费**，不碰退出码判定、重试次数、续录时序 |

---

## 7. 最小修复方案（建议，未实施）

### 方案 A（推荐）：给事件加 `scope`，会话级事件不伪造分段字段

1. `_end_without_segment` 载荷改为：

```python
{
    "scope": "session",          # 新增
    "recording_id": None,
    "room_id": active.room_id,
    "file_path": "",             # 不再是未创建的分段路径
    "status": status.value,
    "file_size": None,           # 不再是 0
    "error": message if failed else "",
}
```

2. `_close_segment` 的载荷补 `"scope": "segment"`（`:905-915`），其余字段不变。

3. `main_window._on_recording_finished` 分支：

```python
if snapshot.get("scope") == "session":
    # 会话结束：不展示文件大小，文案改为“录制会话结束”
    ...
else:
    # 原有分段结束渲染，逻辑不变
```

4. 日志侧：会话结束不再打印 `size=0`，改为打印原因与重试次数。

**改动面**：2 个文件、约 30 行；**无 Schema 变更、无配置键变更、无打包变更**。

### 方案 B（不推荐）：新增独立的 `recording_session_finished` 信号

语义最干净，但要同时改 `app.py` 的信号连接、可能新增测试夹具，且两套信号并存期间容易漏接。收益与方案 A 相同，风险更大。

### 明确不做

- 不改 `recordings` 表结构（不加 `error_message` 列）—— 那属于另一个决策（PR-06）。
- 不改 `_clean_exit_outcome` 的 live/offline/unknown 判定。
- 不改续录次数与退避参数。
- 不修改任何历史标签、Release 附件。

---

## 8. 回归测试要求

| 层级 | 用例 | 断言 |
| --- | --- | --- |
| 服务层 | 直播自然结束、无活动分段 | `recording_finished` 载荷 `scope="session"`、`file_size is None`、`file_path == ""` |
| 服务层 | 续录耗尽 | 同上 + 先有 `recording_error` |
| 服务层 | 等待续录期间手动停止 | 同上 + 数据库无新增 recording 行 |
| 服务层 | 正常分段结束 | `scope="segment"`、`file_size > 0`、`recording_id` 为整数（防止改坏原路径） |
| 表现层 | 会话结束渲染 | 状态栏/日志不含 `size=0`，含「会话结束」 |
| 回归 | 全量 `pytest -q` | 不低于 276 passed，且新增用例全部通过 |

现有测试文件：`tests/test_recording_service.py`（24.5 KB，主战场）、`tests/test_app.py`、`tests/test_states.py`。

---

## 9. 验收标准

1. 复现三类触发场景，日志与状态栏不再出现「completed size=0」。
2. 正常分段结束的展示与 v0.4.0 完全一致（截图对比）。
3. 数据库 `recordings` 行数、`status`、`file_size` 与修复前逐条一致（用同一录制场景对比）。
4. 全量测试通过数 ≥ 基线 276，无跳过项变化。
5. 发布包构建脚本、图标、版本资源不受影响（`build/verify_icon.py` 仍通过）。

## 10. 回滚策略

- 改动集中在 2 个文件、纯新增键与渲染分支，**回滚即 `git revert` 单个提交**。
- 不涉及数据库迁移，旧版本可直接读写同一 `data\` 目录。
- 若浏览器/用户侧缓存了旧文案，仅影响显示，无数据残留。
