# 录制历史状态专项核查（0.3.1 Candidate）

- 日期：2026-11-21
- 性质：**只读核查**；未运行新的真实录制，未修改用户录制文件、数据库与正式配置，未改动任何源码
- 证据来源：候选版 1 数据目录 `release/DouyinLiveRecorder-0.3.1-gui-candidate-win64/data/`（`logs/app.log`、`app.db` 只读副本、`recordings/`、`runtime/ffmpeg_processes.json`）

## 1. 结论摘要

1. 5 条 `failed` 记录的原因可全部解释，**数据库落库内容与录制服务状态机一致，未发现落库时序、日志写入错误或并发竞争导致的数据损坏**。
2. 两条存在产物的记录（id1、id5）被标记 `failed`，是「FFmpeg 退出码 0 后无法确认直播状态」这一保守策略的结果，不是文件损坏，也不是数据库写错。
3. id5 日志中的 `status=completed size=0` **不是数据库值**：它是会话级结束事件复用了最后一个分段的文件路径，而状态与大小为会话语义，导致日志/状态栏与历史表（读数据库）不一致。
4. 该行为位于公共录制核心 `recording_service.py`，候选版 2 同样具备；但**不影响数据库与文件正确性**，不构成候选版 2 的 GUI 验收阻断项。
5. 另有两点观察：`recordings/` 目录当前为空（文件已不在库内记录路径上，非本程序删除）；FFmpeg 的 stderr 被丢弃，非零退出码缺少直接证据。

## 2. 证据基线

| 项目 | 值 |
| --- | --- |
| 数据库 | `app.db` 36,864 B，schema version 2，最后写入 18:46:58 |
| 进程登记表 | `runtime/ffmpeg_processes.json`：`processes: []`，更新于 17:24:01 |
| `rooms` | 0 行（房间 id=1 已被删除，`recordings.room_id` / `monitor_sessions.room_id` 按 `ON DELETE SET NULL` 置空） |
| `monitor_sessions` | 1 行：17:03:23–18:37:05，`status=stopped`，`room_id=NULL` |
| `recordings` | 5 行，状态全部 `failed`（详见第 3 节） |
| `recordings/` 目录 | **当前为空**；目录最后变更时间 18:41:18（录制已于 17:24 结束） |

## 3. 五条记录逐项核查

时间均为本机时区（日志 17:xx = 数据库 `Z` 时间 09:xx）。房间删除后 `room_id` 为 NULL，房间归属以 `web_rid_snapshot` 判定，全部为 `206455805985`。

| ID | 录制区间（时长） | Session | FFmpeg 退出码 | 库内状态 / 大小 | 当时文件 | 归类 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 17:03:26–17:14:56（11 分 30 秒） | 1 | `0` | `failed` / 511,466,220 B | 存在且大小一致；全文件解码 `exit=0` | 中断（未确认直播状态），文件可用 |
| 2 | 17:18:51–17:18:57（6 秒） | 1 | `3753488571`（0xDFB0C0BB） | `failed` / 0 B | 不存在 | 真实录制失败 |
| 3 | 17:21:01–17:21:07（6 秒） | 1 | `3753488571` | `failed` / 0 B | 不存在 | 真实录制失败 |
| 4 | 17:23:11–17:23:17（6 秒） | 1 | `3753488571` | `failed` / 0 B | 不存在 | 真实录制失败 |
| 5 | 17:23:26–17:24:01（35 秒） | 1 | `0` | `failed` / 8,372,580 B | 存在且大小一致；全文件解码 `exit=0` | 中断（未确认直播状态），部分可用 |

对应关系核查：

- 每条 `recordings.file_path` 与日志「开始录制 / 录制失败」输出完全一致；`started_at` / `ended_at` 与日志时间逐秒吻合，无错位或串行。
- 文件名规则 `{显示名或 web_rid}_{web_rid}_{yyyyMMdd_HHmmss}_{随机 6 位}.ts`，时间戳与启动时间一致。
- id2、id3、id4 是「监控轮询重新解析成功后发起的新录制」，id5 是 id4 异常退出后的**自动续录分段**（日志为「自动续录已启动 attempt=1」），因此 id5 与 id4 属于同一录制会话的不同分段。
- 5 条记录全部挂在唯一的 `monitor_sessions.id=1` 下；该会话在 18:37:05 因用户删除房间而被结束（`remove_room` → `stop_monitor` + `delete_room`，该路径不写日志）。

## 4. 两个可解码文件为什么被标记 failed

状态机路径（[recording_service.py](file:///c:/Users/kglpl/Desktop/DouyinLiveRecorder-Desktop/src/dlrecorder/application/recording_service.py#L580-L632)）：

```
FFmpeg 退出码 0 → _clean_exit_outcome() 重新解析直播状态
  ├─ live=true  且拿到新地址 → 按网络中断续录（不算失败）
  ├─ live=false            → 按直播结束收尾（completed）
  └─ live=None（无法确认） → _begin_reconnect(reason=...) → _close_segment(error≠"") → failed
```

- id1：17:14:31 / 17:14:56 两次解析均报 `SSL: UNEXPECTED_EOF_WHILE_READING`（传输层失败），属 `live=None` → 按「无法确认直播状态（网络异常）」结束当前分段并标记 `failed`，大小按磁盘实际值写入 511,466,220 B。随后 5 次自动续录（17:14:56–17:16:53）全部因 `NETWORK_ERROR` 失败，会话结束。
- id5：17:24:01 解析命中抖音风控验证页（`接口未返回房间数据`），同样属 `live=None` → 标记 `failed`，大小 8,372,580 B；7 秒后（17:24:08）重新解析返回 `live=false`（直播已结束），会话正常收尾。

结论：这两条是「FFmpeg 已干净退出、但状态确认链路本身失败」，设计上不允许在无法确认时标 `completed`，因此归类为：

- **录制中断但文件可用**（id1，11.5 分钟、约 5.9 Mbps，可完整解码）；
- **录制中断但部分可用**（id5，35 秒）。

补充说明：全文件解码成功只能证明 TS 容器与码流可读，**不能证明覆盖了完整直播**，本报告未据此判定「录制完整成功」。

## 5. ID 5 的 `completed/size=0` 与库内 `failed/8372580` 差异来源

- 17:24:01 写库：`finish_recording(id=5, "failed", 8372580)` —— 日志「录制失败 … exit=0」与库内值一致。
- 17:24:08 会话收尾调用 [`_end_without_segment(failed=False)`](file:///c:/Users/kglpl/Desktop/DouyinLiveRecorder-Desktop/src/dlrecorder/application/recording_service.py#L819-L841)，该函数：
  - **不写数据库**（此时没有打开中的分段）；
  - 但发出的 `recording_finished` 事件里 `recording_id=None`、`status="completed"`、`file_size=0`，同时 `file_path` 仍指向上一个分段（id5）的文件。
- GUI 侧 [`_on_recording_finished`](file:///c:/Users/kglpl/Desktop/DouyinLiveRecorder-Desktop/src/dlrecorder/presentation/main_window.py#L505-L526) 直接把该事件当作「某条录制结束」记录日志并更新状态栏，于是产生 `房间 1 录制结束 status=completed size=0 file=...af593a.ts`。

同类现象在 id1 也出现过（17:16:53 `status=failed size=0`，库内为 `failed/511466220`），本质相同：**会话级事件使用了分段级字段**。

结论：属于**日志/界面语义缺陷**，不是数据库状态错误。历史表读取 `recordings` 表，5 行仍正确显示「失败」及真实大小；受影响的只有「运行日志」文本与窗口底部状态栏。

## 6. 数据库时序与并发

- 数据库写入只发生在录制管理线程（`recording_service`）与监控工作线程（`monitor_sessions`），两者共用 `Database._lock`，且 `finish_recording` 只对 `status='recording'` 生效（幂等）。
- 5 条记录的 `ended_at` 与日志结束时间一一对应，没有重复更新、回滚或覆盖；`sqlite_sequence` 连续（1→5），无插入丢失。
- 同一时刻只有一个 FFmpeg 进程（`ffmpeg_processes.json` 最终为空），未出现两个进程写同一路径。
- 未发现竞态、落库滞后或事务丢失证据。

## 7. 是否影响候选版 2

- `recording_service.py` 最近一次修改为 `eeb4bb1`，早于 GUI 候选版；候选版 2（`f168880`）相对候选版 1 只改了 `main_window.py` 的托盘异常降级（11 行增、2 行删）。
- 因此候选版 2 **同样会出现**本章描述的 `completed size=0` 日志/状态栏文案，以及「无法确认直播状态即判 failed」的历史结果。
- 但数据库内容、录制文件与进程清理均正确，**不影响候选版 2 的打包冒烟与 GUI 验收结论**。

## 8. 是否需要修复（待 Codex 复核，本轮不实施）

判定：**需要修复，但属低优先级、非阻断**；因涉及录制核心，超出本 GUI 授权范围，仅提交方案。

最小方案：

1. `_end_without_segment` 事件显式标注会话范围（如 `scope="session"`），并**不再复用**上一分段的 `file_path`；或改为携带最近一次分段的真实 `status/file_size`（从 `recordings` 读回）。
2. `main_window._on_recording_finished` 按 `scope` 分支：会话结束只记录「直播间 N 录制会话结束：直播已结束」，不再输出「录制结束，文件 0 字节」。
3. （诊断增强，可选）将 FFmpeg stderr 写入独立日志文件，用于解释 id2/id3/id4 的 `exit=3753488571`。

涉及文件：

- `src/dlrecorder/application/recording_service.py`（`_end_without_segment`、`_handle_stop`）
- `src/dlrecorder/presentation/main_window.py`（`_on_recording_finished`）
- 对应测试：录制状态机/事件契约相关用例，需同步字段断言

回归范围：

- 录制分段结束与自动续录、续录耗尽、直播结束收尾、手动停止（含「等待续录期间」停止）
- GUI 事件到日志/状态栏的映射
- 数据库 `finish_recording` 幂等与历史表展示

风险：事件结构变更影响 GUI 与既有测试契约，须成对修改；不得改变数据库 Schema。

## 9. 其他观察（不改变上述结论）

1. **录制文件当前不在库内路径**：`recordings/` 目录为空（目录最后变更 18:41:18），而库内仍记录两条产物的路径与大小。仓库内不存在删除录制文件的代码路径（历史页明确只读），推测为用户手动移动或删除，需用户确认；本报告沿用录制结束时的文件事实（大小一致、全文件解码通过）。
2. **FFmpeg stderr 丢弃**：`FFmpegProcess.start` 使用 `stderr=subprocess.DEVNULL`，`recordings` 表也无 `error_message` 字段，导致 id2/id3/id4 的 `3753488571`（0xDFB0C0BB）无法从现有证据定性，只能确认「6 秒内 0 字节、拉流/封装未建立」。
3. **房间删除无日志**：`remove_room` 未记录日志，导致 `rooms` 清空与 Session 结束（18:37:05）在日志中不可见。

## 10. 下一步最小验证建议

1. 授权房间开播后做一次 30–60 秒真实录制并**手动停止**：确认历史表 `completed`、大小 > 0，状态栏文案与之一致。
2. 覆盖一次「直播结束收尾」：**修复前**该场景仍会打印 `completed size=0`，验证时只如实记录现象，不得作为失败判据（见第 11 节 P2-01）。
3. 若再次出现非零退出码，先落地 stderr 采集再判定，不重复无证据的猜测。

## 11. 问题登记（2026-11-21 收尾，本轮不修复）

本节为台账登记，不是修复记录。相关代码**均未修改**，候选版 2 原包保持不变。

### 11.1 P2 待修复：会话结束事件与分段结束事件混淆

- 编号：P2-01
- 现象：会话收尾（直播结束 / 续录耗尽 / 等待续录期间手动停止）复用了最后一个分段的 `file_path`，但 `status`、`file_size` 为会话语义（`completed` / `0`），GUI 据此输出「录制结束 status=completed size=0」。日志与历史表（读数据库）不一致。
- 位置：`recording_service._end_without_segment`（事件构造）、`main_window._on_recording_finished`（消费）
- 影响：仅运行日志与状态栏文案；数据库 `recordings` 内容、录制文件与进程清理均正确。
- 当前状态：**未修复**。候选版 2（`f168880`）沿用同一录制核心，同样存在该现象。
- 处理约定：修复需变更事件契约，属录制核心范围，待 Codex 复核后另行授权；**在契约修改落地前，不得将现有版本视作已修复，也不得用修复后才成立的断言要求未修改的候选包通过。**

### 11.2 后续可靠性 / 诊断问题（不在缺少证据时推断根因）

| 编号 | 问题 | 现有证据 | 未定性原因 |
| --- | --- | --- | --- |
| R-01 | FFmpeg 三次未知退出（id2/id3/id4，`exit=3753488571` / 0xDFB0C0BB） | 启动后 6 秒退出、输出 0 字节、无残留进程 | stderr 被丢弃，`recordings` 无 `error_message` 字段，无法判定是拉流拒绝、鉴权失效还是进程异常终止 |
| R-02 | 解析侧 SSL 连接异常（`SSL: UNEXPECTED_EOF_WHILE_READING`） | 17:14 起多次出现，导致 `live=None`（无法确认直播状态） | 未区分网络抖动、TLS 中断与服务端主动断连；无抓包/重试样本 |
| R-03 | 风控验证页中断（`接口未返回房间数据`） | 17:24:01 命中验证页，7 秒后解析恢复正常 | 未确认触发条件（频率/IP/匿名访问策略），不做根因推断 |
| D-01 | 诊断信息缺失 | 日志无 FFmpeg stderr；历史表无失败原因列；`remove_room` 无日志 | 属可观测性缺口，非功能缺陷 |

以上均登记为后续处理项，本轮不修改、不新增测试断言、不重复扫描。
