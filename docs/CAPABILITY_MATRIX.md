# 第二阶段能力对标矩阵（0.1.1 → 第二阶段）

- 编写日期：2026-11-18
- 对标对象 A（本项目）：DouyinLiveRecorder-Desktop 0.1.1（Tag `v0.1.1` = `abcc7e0`）+ 第二阶段改动（工作区，未提交、未发布）
- 对标对象 B（上游）：`ihmily/DouyinLiveRecorder`，commit `add187f8d8c7ff7d231fcbee45cbb4f1ed247d3a`（2025-11-03，Release `v4.0.7`）
- 实测基线：[CAPABILITY_BASELINE.md](CAPABILITY_BASELINE.md)（同一房间、同一网络、同一 FFmpeg 构建）
- 上游代码溯源：[UPSTREAM_AUDIT_0.1.1.md](UPSTREAM_AUDIT_0.1.1.md)

> 判定口径：只把**可复现测试或代码事实**当作证据；"功能命名相同"不算已实现。状态分四类：
> **已验证** / **已实现未充分验证** / **未实现** / **有意不采用**（明确不在产品范围内）。

---

## 1. 总览

| # | 能力 | 上游实现（`add187f8`） | 本项目现状 | 测试覆盖 | 差距与风险 | 借鉴 | 本轮方案 | 验收标准 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 抖音链接识别（长链/短链/主页） | `room.py:52/78/109` 短链、主页、live 链接解析 | [models.py:26](../src/dlrecorder/domain/models.py#L26-L53) 仅长链 `web_rid`；短链只提示 | `test_db.py`、`test_app.py` | 短链/主页链接需用户手工打开，属于输入体验差距；低风险 | 否（仅补解析规则） | 暂不处理 | 粘贴短链可直接添加房间 | 未实现 |
| 2 | 房间状态解析与开播判定 | `spider.py:68/145/230` 三路回退 | [parser.py:153](../src/dlrecorder/douyin/parser.py#L153-L237) 公开页 React Flight 单源 | `test_parser.py` 26 项、`test_states.py` | 当前可用；页面结构变化即失效，中高风险 | 部分（状态语义已一致） | 保持现状，补第二数据源另立任务 | 连续 30 次解析成功；改版时自动回退 | 已验证（单源）；抗改版未实现 |
| 3 | 签名与风控能力 | `src/ab_sign.py` + `x-bogus.js` + PyExecJS | 无 | 无 | 遇到风控/签名校验即无法解析；中高风险 | 不直接复用（依赖 Node/JS，体积与许可成本） | 评估纯 Python 签名与官方接口可行性 | 风控场景下仍能解析 | 未实现（评估中） |
| 4 | 解析效率与轮询成本 | JSON 接口，中位 504 ms | 整页 1.34 MB/次，中位 1478 ms（**2.9×**） | 基线 30 次实测 | 单房间约 0.96 GB/日；开播发现延迟被放大；中风险 | 是（接口形态） | 评估轻量接口/条件请求（ETag/Last-Modified） | 中位 ≤700 ms 或单次 ≤300 KB | 已实现未优化 |
| 5 | 流地址清晰度选择与可用性探测 | `stream.py:41` 多档映射 + 探测换档 | [parser.py:64](../src/dlrecorder/douyin/parser.py#L64-L82) 固定键序取第一个 | `test_parser.py` | 拿到不可用地址时无备选；中风险 | 是（探测思路） | 记录候选地址列表 + 失败后换档（需真实样本验证） | 首个地址失败时自动换档并起录 | 未实现 |
| 6 | FFmpeg 输入健壮性（超时/重连参数） | `main.py:1178-1192` `-rw_timeout`、`-reconnect_*` | 本轮 [ffmpeg.py:104](../src/dlrecorder/infrastructure/ffmpeg.py#L104-L152) 增加 `-rw_timeout 15s`、`-reconnect 1`、`-reconnect_delay_max 10`（仅 HTTP(S) 输入，位于 `-i` 前） | `test_ffmpeg.py` 10 项；10 min 真机 0 丢包 / 0 断层 | 上游把重连参数放在 `-i` 之后（实际作用于输出，写法可疑）；本项目按输入侧正确位置实现 | 是（参数与取值） | 已实现；弱网断开场景待专项验证 | 弱网断开后 FFmpeg 不退出、文件持续增长 | 已实现未充分验证（含 10 min 无回归） |
| 7 | 录制进程异常退出后续录 | `main.py:420 check_subprocess` + 外层循环重新解析、新建文件 | 本轮 [recording_service.py:491](../src/dlrecorder/application/recording_service.py#L491-L538) 有界重试 + 重新解析 + 新文件；`recording_reconnecting` 信号 | `test_recording_service.py` 新增 4 项；强杀实测见 §3 | 基线实测：本项目 150 s 内 0 次拉起并置 `failed`；上游 27.6 s 续录 | 是（行为） | 退避 2/5/10/20/30 s，最多 5 次；退出码 0 视为自然结束不续录 | 强杀 FFmpeg 后 ≤35 s 内出现新文件并继续增长 | 已验证（真机 4.9 s 续录） |
| 8 | 流地址过期后的刷新 | 外层循环每次重新解析 | 本轮续录前通过 [app.py:130](../src/dlrecorder/application/app.py#L130-L147) 重新解析 | 上述 4 项（桩解析器） | 真实 `expire` 到期未验证；中风险 | 是 | 每次续录都重新解析，不复用旧地址 | 地址过期后仍能续录 | 已实现未充分验证 |
| 9 | 直播结束识别与收尾 | `status=4` / FFmpeg 退出后等待下一轮 | [recording_service.py:539](../src/dlrecorder/application/recording_service.py#L539-L571) 重新解析 `status=4` → 结束会话；退出码 0 → `completed` | 新增 `test_confirmed_offline_during_resume_ends_session` | 结束与断网通过退出码区分；误判风险低 | 是（语义） | 已实现 | 下播后不反复拉起，历史状态为 `completed` | 已验证（桩） |
| 10 | 启动即值守 / 自动恢复 | 启动读取 `URL_config.ini` 全部房间并开始录制 | 自动录制仅在用户已启动监控的房间上生效；重启后不自动恢复监控 | `test_app.py`（自动录制逻辑） | 关闭程序再打开不会自动继续值守；中高风险（值守产品核心体验） | 是（行为） | 需产品确认后实现"启动时自动恢复监控" | 重启后自动恢复监控与录制 | 未实现（需确认） |
| 11 | 手动停止与优雅收尾 | stdin `q`，10 s 兜底 | [ffmpeg.py:185](../src/dlrecorder/infrastructure/ffmpeg.py#L185-L262) `q → terminate → kill`，如实报告 | `test_ffmpeg.py`、`test_recording_service.py` | 基线实测 0.28 s 收尾，优于上游 0.52 s | 否 | 保持 | 停止后文件可播放、状态准确 | 已验证 |
| 12 | 磁盘空间保护 | 配置阈值 + 录制前检查 | [recording_service.py](../src/dlrecorder/application/recording_service.py) 启动前与轮询中检查；续录前再检查 | `test_disk_protection.py` 8 项 | 与上游等价，且覆盖续录路径 | 否 | 保持 | 低空间时停止录制且不丢已写文件 | 已验证（桩） |
| 13 | 录制历史与可追溯性 | 无数据库，仅文件 | SQLite 历史、`recording`/`completed`/`failed`/`interrupted` | `test_db.py`、`test_recording_service.py` | 本地方向优于上游；续录产生多个分段记录 | 否（本地新增） | 每个分段一条记录，信号带 `resumed` 标记 | 历史可定位每个文件与失败原因 | 已验证（桩） |
| 14 | 多房间并发录制 | 线程池 + semaphore（默认 3） | 单管理线程 + 每房间子进程，无全局并发上限 | `test_recording_service.py` 双房间用例 | 未做 10+ 房间压测；单线程轮询在同步解析时会被阻塞（≤15 s） | 部分（上限思路） | 记录为已知限制，后续压测后决定是否加信号量 | 10 房间并发 10 min 无漏录 | 已实现未充分验证 |
| 15 | 分段录制 | `main.py:1254-1544` 按大小/时长切段 | 仅在异常续录时新建文件，无主动分段 | 无 | 长录制单文件过大；低风险（TS 本身可播） | 可选 | 暂不实现 | — | 有意不采用（本期） |
| 16 | 转 MP4 / 转码 / 字幕 / 推送 / 代理 | `main.py:219/254/273/356`、`msg_push.py`、`proxy.py` | 无 | 无 | 超出"抖音单平台桌面录制"范围 | 否 | 不做 | — | 有意不采用 |
| 17 | 配置管理、备份与单实例 | 配置文件 + 备份脚本，无单实例 | SQLite 设置、SQLite Backup API、命名互斥量 | `test_settings.py`、`test_backup.py`、`test_single_instance.py` | 本地方向优于上游 | 否（本地新增） | 保持 | 设置可校验、备份可恢复、双开被阻止 | 已验证 |

---

## 2. 本轮改动范围（可回退）

| 文件 | 改动 | 风险控制 |
| --- | --- | --- |
| [ffmpeg.py](../src/dlrecorder/infrastructure/ffmpeg.py#L104-L152) | `build_record_command` 增加可选 `reconnect/rw_timeout/reconnect_delay_max/max_muxing_queue_size`；仅 HTTP(S) 输入启用重连，本地文件不加 | 关键字参数默认值可关闭；原有调用方式不变 |
| [recording_service.py](../src/dlrecorder/application/recording_service.py) | 新增 `StreamRefresh`、`recording_reconnecting` 信号、有界重试状态机（`process=None` 表示等待续录）；续录新建文件与独立历史记录 | 默认最多 5 次、失败后仍置 `failed`；停止/关闭会取消等待中的重试 |
| [app.py](../src/dlrecorder/application/app.py#L130-L147) | 注入 `stream_resolver`：续录前重新解析房间 | 解析异常只影响本轮续录，不抛出到管理线程 |
| [main_window.py](../src/dlrecorder/presentation/main_window.py#L324-L336) | 显示"录制中断，N 秒后自动续录" | 仅状态栏与录制标记，无流程改动 |
| 测试 | 新增 7 项（命令参数 3、续录/有界/下播/停止等待 4） | 全量 `pytest -q` 通过 |

未改动：数据库 Schema、已发布 Tag `v0.1.1`、发布包、用户数据与凭据。

---

## 3. 关键能力证据

### 3.1 异常退出续录（第 7 项）

- 单元/集成（桩进程）：`tests/test_recording_service.py`
  - 非零退出 → 第一个分段 `failed`（原因含"正在自动续录"）→ 退避后新文件、新记录、`recording_started(resumed=True)`；
  - 连续失败 2 次后停止，最终原因含"已尝试 2 次"，不残留 `recording` 状态；
  - 重新解析确认下播 → 不再拉起，会话以 `completed` 结束；
  - 等待续录期间手动停止 → 取消重试，不创建空分段。
- 真实网络强杀（2026-11-18 13:38–13:42，`web_rid=646379392926`，`taskkill /F`）：
  - 60.6 s 强杀（分段 1 已写 51.1 MB）→ 62.4 s 识别退出（1.8 s）→ 62.4 s 进入退避 → **65.5 s 新文件起录（杀后 4.9 s）**；
  - 续录分段持续增长 120 s（96.6 MB），停止后状态 `completed`；
  - 数据库：分段 1 `failed`（原因"FFmpeg 退出码 1，正在自动续录"）、分段 2 `completed`；
  - `shutdown` 后 `remaining_active=0`，管理线程正常退出。
  - 复现：`%TEMP%\dlr-baseline\kill_local_resume.py`，原始数据 `results\kill_local_resume.json`。
  - 局限：续录分段的 TS 连续性未做逐包分析（临时目录随进程清理）；未覆盖真实网络抖动（仅强杀）。

### 3.2 录制命令（第 6 项）

- 输入侧参数位于 `-i` 之前；本地文件输入不添加 `-reconnect`（FFmpeg 会对未知选项直接报 `Option not found`，已由 `test_build_record_command_skips_http_options_for_local_file` 固化）。
- 不使用 `-reconnect_at_eof` / `-reconnect_streamed`：直播结束时它们会反复重连已结束的流，使进程无法退出；自然结束由外层状态机处理。

### 3.3 解析效率（第 4 项）

- 基线：本项目 1478 ms / 1.34 MB 每次；上游 504 ms / JSON。
- 这是**成本与时延**差距，不是当前成功率差距（双方 30/30）。优化需引入轻量接口或条件请求，涉及数据源变更，单独评估，不与录制层重构混做。

---

## 4. 优先级与后续顺序

| 优先级 | 事项 | 理由 |
| --- | --- | --- |
| P0（本轮已完成） | 异常退出续录、输入侧重连参数、下播识别 | 基线中唯一定性差距，直接影响录制完整性 |
| P1 | 启动即值守（第 10 项） | 值守产品核心体验；需产品确认行为 |
| P1 | 解析效率（第 4 项） | 流量与开播发现时延；改动面在解析层 |
| P2 | 第二数据源/签名（第 2、3 项） | 抗改版能力；依赖外部验证，风险最高 |
| P2 | 清晰度探测（第 5 项） | 需要真实多档地址样本 |
| P3 | 短链识别（第 1 项）、并发压测（第 14 项） | 输入体验与容量验证 |

---

## 5. 验收口径

本轮交付以以下条件为准，缺一不算完成：

1. `pytest -q` 全量通过（当前 216 项）。
2. 强杀 FFmpeg 后，服务在重试预算内自动重新解析、新建文件并继续增长；若预算耗尽，历史状态必须为 `failed` 且原因可读。
3. 下播/地址失效不会被误判为"录制成功"；退出码 0 与异常退出码的行为有明确区分。
4. 未修改数据库 Schema、未修改 Tag `v0.1.1`、未写入任何凭据。
5. 所有"未验证"项在阶段报告中显式列出，不以"代码存在"代替验证。
