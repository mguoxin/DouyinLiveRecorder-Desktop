# 项目开发状态记录

更新时间：2026-11-28（v0.4.2 最终候选验证）
项目：DouyinLiveRecorder-Desktop（Windows 抖音直播监控与录制）
维护方式：每个有意义的开发阶段结束时更新；以实际代码和测试结果为准。

## 0. 当前状态（2026-11-28）

| 项 | 当前状态 | 证据 |
| --- | --- | --- |
| 源码版本 | **0.4.2（候选）**；正式发布版仍为 **v0.4.1** | `pyproject.toml`、`src/dlrecorder/__init__.py`、EXE 版本资源 `0.4.2.0` |
| 整改分支 | `fix/v0.4.2-remediation-4` = `688147b`（领先 v0.4.1 共 14 个提交，已推送 `origin`，未合并） | `git log v0.4.1..HEAD` |
| 标签 | `v0.1.0`～`v0.4.1` 共 7 个；**无 v0.4.2 标签** | `git tag` |
| 数据库 | SQLite Schema V2，本轮未变更 | `infrastructure/db.py` |
| 全量回归 | **415 passed / 0 failed / 0 skipped**（2026-11-28，HEAD `688147b`） | 本地 `pytest -q`；`_audit/r4_delivery/` |
| 远端 CI | GitHub Actions `pytest (Windows, Python 3.12)`：**success**（run 46104506413，head `688147b`） | GitHub API / Actions 页面 |
| 候选包 | `dist/DouyinLiveRecorder-v0.4.2-candidate-r4.zip`，109,139,779 字节，SHA-256 `38EF9A0A23EACA3C4BFA0DF883B169CCC74D366B5FDDCC23AA2ECF0A8112B9FB` | `_audit/r4_delivery/evidence/package_sha256.txt` |
| 打包闭环 | 11 步本地合成流全部通过（添加/监控/录制/强杀续录/停止/退出/重启） | `_audit/r4_delivery/evidence/e2e_run.log` |
| 稳定性 | 30 分钟打包程序观察：30 次采样、0 停滞、0 错误、退出码 0、无残留 FFmpeg；文件 50,294,136 B 与 DB 一致 | `_audit/r4_delivery/evidence/soak_run.log`、`soak_db.json` |
| 真实抖音 | 匿名只读解析通过（两个公开房间 `status=4` 未开播、无效 ID 不误判）；**直播录制 BLOCKED** | `_audit/r4_delivery/evidence/real_parse_probe*.log` |
| 尚未验证 | 真实抖音直播录制、24 小时 soak、低配 i3/4GB、Windows 10、打包内诊断导出/托盘交互 | 见最终验收报告 §未完成项 |
| 发布结论 | **CONDITIONAL PASS**：代码、回归、打包闭环与远端 CI 通过；真实直播与 24h 长稳未闭环，暂不建议公开发布 | `_audit/r4_delivery/V0.4.2-FINAL-DELIVERY-REPORT.md` |

> 说明：`CHANGELOG.md` 提供完整版本沿革；下方 §0A～§10 为 **0.3.1 时期（2026-11-18）的历史快照**，
> 保留用于追溯，不代表当前版本状态。历史测试数字、打包路径与功能结论请勿直接引用为 v0.4.2 结论。

## 0A. 历史版本记录（截至 0.3.1，2026-11-18）

- 当前版本：**0.3.1**（`src/dlrecorder/__init__.py`、`pyproject.toml`），0.3.0（批次 F/G/H）之上的链接解析修复版本。
- 版本号会写入窗口标题与启动日志 `应用初始化，版本=...`，用于核对打包程序与源码一致。
- 源码基线：Git 仓库（`master`），已有标签 **`v0.1.0` / `v0.1.1` / `v0.2.0` / `v0.3.0`**（均保持不变）；
  0.3.0 在 `v0.2.0` 基础上继续开发，交付说明见 `docs/DELIVERY_0.3.0.md`，
  测试记录见 `docs/TEST_REPORT_0.3.0.md`，0.2.0 迁移见 `docs/MIGRATION_UPSTREAM_0.2.0.md`；
  0.3.1 修复 `www.douyin.com/follow/live/<数字>` 链接无法识别的问题，见 `docs/FIX_0.3.1_URL_PARSING.md`。
- 仓库不跟踪 `.venv`、`_probe_gui`、`_test_artifacts`、`acceptance`、`data`、`dist`、`release`
  与 `tools/ffmpeg/bin/*.exe`（FFmpeg 版本与校验和记录在发布报告中）。

## 1. 当前基线

- Python 3.12.16（项目独立虚拟环境 `.venv`）。
- PySide6 Widgets + SQLite（Schema V2，未变更）+ httpx(HTTP/2) + FFmpeg 子进程。
- 入口：`.venv\Scripts\python.exe -m dlrecorder`。
- 测试：`.venv\Scripts\python.exe -m pytest -q`。
- 最近完整回归：**276 passed**（2026-11-18，0.3.1 链接解析修复后）。
- 单实例、数据库历史数据保护、渐进式改造原则保持不变。
- 生产默认装配 `EngineDouyinParser`（上游 `webcast/room/web/enter` + `a_bogus`），并以
  `RealDouyinParser`（公开页 React Flight）作为第二数据源回退；`DLR_PARSER=mock` 仅供开发/自动化验证。

## 2. 已完成功能

| 模块 | 状态 | 说明 |
| --- | --- | --- |
| 房间管理 | 已完成 | 添加 / 编辑 / 删除、参数校验、唯一约束、持久化；监控中禁止修改直播间 ID |
| 监控任务 | 已完成 | 多房间并发、任务隔离、Session 生命周期、停止与异常取消处理；排队已满仅提示一次并自动重试 |
| 单实例 | 已完成 | Windows 命名互斥量；进程终止时由系统回收 |
| HTTP 层 | 已完成 | 专用执行器、关闭串行化、传输错误分类；真实 HTTPS 请求已实测可达 |
| 解析器 | 已完成（0.2.0 切换上游引擎） | 主链路 `web enter + a_bogus`（约 4 KB JSON，0.39–0.78 s/房间），回退公开页 React Flight；风控单独归类 `RISK_CONTROL`；实测离线房间 `status=4 -> OFFLINE_CONFIRMED`、无效 ID 不误判直播 |
| FFmpeg 管理 | 已完成（0.3.0 生命周期） | 启停、q/terminate/kill、退出码、输出增长检测、卡死超时；Job Object 保证应用被强杀时子进程随之终止 |
| 进程登记与孤儿清理 | 已完成（0.3.0） | `data/runtime/ffmpeg_processes.json` 记录 (PID, 创建时间, 路径)；启动时仅在三项身份完全匹配时终止遗留 FFmpeg，其余跳过并保留记录 |
| 网络中断恢复 | 已完成（0.3.0） | 退出码 0 但无法确认直播状态、输出长时间不增长时重新解析续录；重试 5 次、退避 2/5/10/20/30 s，有界不无限重连 |
| 录制清晰度 | 已完成（0.3.0） | OD/UHD/HD/SD/LD，默认 UHD；设置持久化，下一次解析生效，上游自动回退 |
| Cookie 登录 | 已完成（0.3.0） | Windows DPAPI 加密保存于 `data/runtime/douyin_cookie.bin`；不入库、不回显、不进日志；失效时归类风控并提示 |
| 真实 FFmpeg 录制 | 已完成（0.2.0 真实验证 2026-11-18） | 内置 FFmpeg 9.0.2；真实抖音房间 `476806339740` 源码版录制 61 s：19,214,916 B；打包版录制 205 s：63,374,236 B；两次 DB 大小与磁盘一致、全解码退出码 0、无残留进程 |
| 录制服务 | 已完成 | 状态机、历史持久化、异常恢复（interrupted）、多房间独立 |
| 自动录制 | 已完成 | 确认 LIVE 后启动；确认下播 / 停止监控 / 删除房间时停止；开关持久化 |
| 录制历史 | 已完成 | 只读历史对话框；不提供误删入口；文件路径保留 |
| 录制-Session 关联 | 已完成（含真实录制） | 录制记录写入 `session_id`，可追溯到对应监控会话 |
| 稳定性冒烟 | 已完成（离线） | 10 房间并发启停、同房间 5 轮循环启停，Session 全部正常结束 |
| Windows 打包 | 已完成（本机验证） | PyInstaller 6.22.3 onedir，内置 FFmpeg 9.0.2；已重建包含真实解析修复 |
| 打包 GUI 端到端 | 已完成（本地媒体 + 真实直播流） | 真实窗口操作：添加房间、LIVE/SUCCESS、自动录制、历史已完成、退出码 0；真实房间 `95694179922` 录制 23,837,836 字节 TS |
| 软件设置 | 已完成 | FFmpeg 路径 + 可用性检测、录制目录、监控间隔、磁盘阈值；持久化并可运行时应用 |
| 磁盘空间保护 | 已完成 | 低于阈值阻止开始录制；录制中低于阈值停止并标记失败；不删文件 |
| 数据备份 | 已完成 | SQLite Online Backup API 一致性备份 + 完整性校验 |
| 数据恢复 | 已完成 | 下次启动应用、恢复前安全副本、失败保留当前库；不删录制文件 |
| GUI | 已完成 | 房间列表、编辑、录制控制、自动录制、历史、设置、备份/恢复、日志 |
| 性能基准工具 | 已完成（本机） | `tools/benchmark.py`：20 房间并发、初始化/内存/关闭指标，并输出 CPU/内存信息 |
| 长时稳定性工具 | 已完成 | `--soak-hours` 每分钟写入 JSONL（内存、LIVE 数、监控数、告警数、线程数）；24h 采样已完成 |
| 稳定性修复 | 已完成 | 修复日志桥接在窗口销毁后仍向已删除 QObject 发信号导致访问冲突的问题 |
| 磁盘检查健壮性 | 已完成 | 录制目录被外部删除时按上级目录所在卷判断，同一路径只告警一次 |
| 数据目录定位 | 已完成 | 打包运行固定使用 EXE 同级目录，支持 `DLR_DATA_DIR` 覆盖；源码运行保持 cwd |
| 启动失败提示 | 已完成 | 目录不可写/数据库损坏时弹出可读提示并返回退出码 4 |
| 录制收尾真实性 | 已完成 | 无法确认 FFmpeg 退出时标记 `failed`，不误记为完成 |
| 备份/恢复一致性 | 已完成 | WAL 存在时使用 SQLite Backup API 复制，安全副本含已提交数据 |

## 3. 正在实施

- 无（24 小时长时稳定性采样已完成，结论见第 6 节）。

## 4. 已决策事项与未完成事项

已决策（2026-11-16，项目所有者确认）：

1. 房间不存在映射：**保持 `API_MALFORMED`**，不单独映射 `ROOM_NOT_EXIST`。依据：无效 ID 返回 200 但无目标房间数据；
   对照探测显示存在但未开播的房间仍有房间对象（`status=4`），但“无房间数据”也可能来自页面结构变化或风控页，
   因此不做语义猜测，统一按解析异常处理，不会误开录制。
   - 对照证据（匿名会话，同一次探测）：
     - `web_rid=111111111111111111`（不存在）：页面完整返回（约 957KB）、无 `<title>`、无目标房间对象；
     - `web_rid=1`（存在但未开播）：有房间对象，`status=4`、`status_str="4"`、无流地址；
     - `web_rid=95694179922`（直播中）：有房间对象，`status=2`。
2. 录制文件保留：**不自动删除任何录制文件**。当前实现即符合该策略，无需新增清理逻辑。

未完成：

3. 低配机器（i3 / 4GB）性能验收（需在目标机器运行 `tools\acceptance_low_spec.ps1`，回传 JSON）。

## 5. 已知缺陷与风险

- 旧 `web/enter` 未签名时实测返回 `200 + 空响应`；生产解析器已不依赖该接口，不会把空响应误判开播。
- 公开页数据位于 React Flight 连续记录中，当前已适配 `T<长度>,` 文本前缀、跨脚本块及嵌套房间对象；页面结构后续变化可能需要同步维护。
- 页面缺少目标房间数据时返回 `API_MALFORMED`，不擅自判定下播或房间不存在。
- 运行中数据文件被外部强制删除时应用无法感知（SQLite 仍写入已删除文件）。正式数据目录不应放在 `%TEMP%`
  等会被清理工具处理的位置；24h 采样末期即因临时目录被清理而出现该情况，未影响内存与监控状态。
- `status=4 -> OFFLINE_CONFIRMED` 依据 2026-11-16 实测样本（无流地址、早期课程标题）；若后续发现其他语义需重新验证。
- 20 房间、1 秒间隔持续运行时仍可能出现瞬时 `queue_full`（执行器容量为 8）；
  现已改为每个房间只提示一次并继续自动重试，不中断监控、不刷屏。
- 监控间隔修改只对之后启动的监控任务生效；录制目录修改只对新录制生效，不移动已有文件。
- 自动录制开启时，用户在直播中手动停止录制，下一个监控周期会再次自动开始（“直播中即录制”语义）；
  如需长期停录，请关闭自动录制开关或先停止监控。

## 6. 测试结果

- 完整回归：**209 passed**（2026-11-18 0.1.1 修复，离线 29.39s）。
  - 0.1.1 新增 4 项链接输入回归：
    `test_validate_web_rid_accepts_live_urls`、`test_validate_web_rid_short_link_gives_actionable_hint`、
    `test_add_room_accepts_full_live_url`、`test_update_room_accepts_full_live_url`。
- 完整回归：**205 passed**（2026-11-17 交付前审查，离线 31.78s）。
  基线 197 项全部保留；新增 8 项：
  - `test_default_base_dir_uses_executable_when_frozen` / `test_default_base_dir_prefers_data_dir_override` /
    `test_default_base_dir_uses_cwd_for_source_run` / `test_application_honors_data_dir_override`；
  - `test_start_monitor_button_reflects_backend_state`；
  - `test_startup_failure_is_reported_to_user`；
  - `test_unconfirmed_process_exit_is_not_marked_completed`；
  - `test_database_copy_includes_wal_data`。
- 交付前打包验证（2026-11-17）：
  - `dist\DouyinLiveRecorder\DouyinLiveRecorder.exe` 已按本轮修复重建（版本 0.1.0）。
  - GUI 冒烟（`acceptance_low_spec.ps1 -Mode gui`，40s/5s）：8 条采样，峰值 96.81MB、增长 3.93MB、
    CPU 平均 0.01%，关闭窗口退出码 0，`gui.passed=true`；数据写入隔离目录 `acceptance\gui-data`。
  - 干净环境：把整套程序复制到 `%TEMP%\dlr-clean-app`，从 `%TEMP%` 作为工作目录启动，
    `data\app.db` 正确创建在 EXE 同级目录、未写入 cwd，退出码 0。
  - 启动日志确认：`版本=0.1.0`、`FFmpeg 路径=...\_internal\ffmpeg\bin\ffmpeg.exe`、`解析器: RealDouyinParser`。
  - 开发机 20 房间基准回归（Mock，1s）：20/20 LIVE，峰值 59.52MB，关闭 0.014s（非低配验收）。
- 指定真实房间（用户批准的 `web_rid=95694179922`，匿名会话）：
  - 页面确认 `roomId=7708721029099178787` 与 `web_rid` 匹配；生产解析器连续两次得到 `SUCCESS / status=2 / 有流地址`。
  - 第一次隔离录制 5.09s，TS 文件 5,116,044 字节，历史 `completed`，Session 关联正确。
  - 第二次隔离录制 4.78s，TS 文件 1,139,844 字节，历史 `completed`，Session 关联正确；FFmpeg 解码确认包含视频和音频。
  - 两次录制管理线程均正常退出、无剩余活动任务；临时测试数据已清理，未读取或改写正式 `data/app.db`。
- 真实状态对照（匿名会话，每个房间连续两次请求）：
  - `web_rid=95694179922`：`status=2`、`status_str="2"`、有 FLV/HLS 流地址（直播中）；
  - `web_rid=1`：`status=4`、`status_str="4"`、`web_stream_url=null`（未开播，标题为早期课程）；
  - `web_rid=999999999999999999`：200 但无目标房间数据，归类 `API_MALFORMED`，不会误判直播。
- 长时稳定性：首批采样在 20 房间、1 秒间隔下运行 6 分钟后观测到 `queue_full` 集中告警（约 64 次事件）；
  已修复为每个房间仅提示一次并继续自动重试，防止用户界面被瞬时过载刷屏；
  修复后 195 项回归通过，随后重新启动 24 小时采样（`%TEMP%\dlr-soak\soak-24h-20261116c.jsonl`）。
- 24 小时采样结论（20 个 Mock 房间、1 秒间隔、每分钟采样，1440 条）：
  - 内存 34.33–59.95MB，平均 36.29MB，无增长趋势；线程数最大 7；LIVE 与监控房间全程 20/20；采样间隔无缺口。
  - 前 0.35h 共 40 条告警（每个房间一次 `queue_full` + 一次界面提示），此后 21.5 小时内保持 40，不再增长。
  - 第 21.94h（约 18:55）起告警计数快速增长，定位原因为**外部临时目录清理工具**删除了运行中的
    `%TEMP%\dlr-soak-*\data\recordings` 与 `app.db`，磁盘检查每秒产生一条带堆栈的 WARNING；
    同目录原 `app.log` 一并被删除（现存日志从 20:09:43 才重新开始），因此计数 7360 大于现存日志中可检索的 2950 条。
  - 监控与状态判定未受影响（20/20 全程 LIVE），内存与线程数无变化。
  - 该现象暴露真实缺陷：录制目录被外部删除时磁盘检查会刷屏。已修复
    [disk.py](../src/dlrecorder/infrastructure/disk.py)：
    路径不存在时回退到最近的已存在上级目录读取卷剩余空间，同一路径只告警一次；
    新增 2 项回归测试，并复测“运行中删除录制目录”场景 70 秒，告警计数保持 0。
- 真实 FFmpeg（离线）：本地媒体录制完成；限速 HTTP 流录制后手动停止，均为 `completed` 且文件非空。
- 打包 GUI 实测（隔离临时目录 `%TEMP%\dlr-gui\appdata`，本地媒体 Mock 流）：
  - 添加房间 `123456789 / GUI验证房间`；状态 `LIVE / SUCCESS`；
  - 自动录制生成 `GUI验证房间_123456789_20261116_195611_8c3cac.ts`，2,566,200 字节；
  - 历史显示“已完成 / 2.4 MB”；主窗口关闭后进程退出码 0。
- 打包 GUI 真实端到端（隔离临时目录 `%TEMP%\dlr-real-gui`，真实解析器 + 真实抖音流）：
  - 启动 `dist\DouyinLiveRecorder\DouyinLiveRecorder.exe`，添加房间 `95694179922 / 真实房间验证`，勾选自动录制并开始监控；
  - 约 7 秒后界面显示 `LIVE / SUCCESS`、监控中=是、录制中=是，日志记录 `FFmpeg 已启动 pid=6716`（`解析器: RealDouyinParser`）；
  - 约 33 秒后停止录制，状态栏“房间 1 录制结束，文件 23837836 字节”；录制历史显示“已完成 / 22.7 MB”，文件路径完整；
  - 停止监控后关闭主窗口，进程 `pid=21400` 退出码 0；
  - 数据核验：`rooms(1,'95694179922')`、`recordings(completed,23837836,session_id=1)`、`monitor_sessions(stopped)`，日志 `录制完成 ... size=23837836 requested=True`；
  - 全程使用隔离工作目录，未读取或改写正式 `data/app.db`。
- 0.2.0 打包版真实直播录制（隔离目录 `%TEMP%\dlr-p0\pkg_e2e`，真实房间 `476806339740`）：
  - 启动 `release\DouyinLiveRecorder-0.2.0-win64\DouyinLiveRecorder.exe`，日志确认 `解析器: EngineDouyinParser`；
  - 界面选中第 3 行后点击“开始监控”，18:47:31 自动开始录制（`pid=20052`），仅 1 次；
  - 18:50:56 关闭主窗口触发收尾，`completed` 63,374,236 B（205 s）；
  - 文件 `Duration 00:03:28.31`，H.264 High 1088x1920 22fps + AAC LC 48kHz；
  - DB：`recordings.id=2 / status=completed / file_size=63374236 / session_id=5`，与磁盘一致；
  - 全解码 `ffmpeg -v error -i ... -f null -` 退出码 0（47 条 DTS 告警均位于 0.02–0.03 s 起始处），FFmpeg 残留 0；
  - 证据详见 `docs/DELIVERY_0.2.0.md` §4。
- 0.3.0 批次 F/G/H 验证（2026-11-18，隔离目录 `%TEMP%\dlr-f|g|h`，详见 `docs/TEST_REPORT_0.3.0.md`）：
  - F：录制中 `os._exit(1)` 崩溃后 FFmpeg 随 Job 立即消失，已写文件保留可解码；重启清理陈旧登记并回填
    `interrupted`；伪造“登记匹配”的遗留 ffmpeg 被安全终止；创建时间/路径不一致时跳过、不误杀。
  - G：真实房间 `476806339740` 五档清晰度（OD/UHD/HD/SD/LD）均 `status=2` 且有流，`selected` 与请求一致；
    HD 真实录制 22 s / 6,666,104 B，H.264 720x1270 22fps + AAC，全解码 0 错误；Cookie 经 DPAPI 加密
    （38 字符），重启读取一致、文件不含明文、清除后回到匿名；伪造 Cookie 触发 `RISK_CONTROL` 且不误开录制。
  - H：4 房间并发 30 分钟（内存 59.6MB 平稳、FFmpeg 合计约 70MB、输出约 1.0GB 持续增长）；
    强杀其中一个 FFmpeg 后该房间自动续录（新分段 5.5MB），其余房间继续增长，结束一致性校验通过；
    网络 8 秒中断不断流（1.0→8.5MB）；90 秒中断触发退出码 0 → 重新解析 → 续录（修复前会直接结束）；
    不可达流地址 6 次尝试、105 秒、最终 `failed`，无残留进程。
  - H 低配模拟：2 个逻辑核亲和性下 20 房间基准 初始化 0.075 s、启动 2.03 s、峰值 63.4MB、关闭 0.013 s、
    20/20 LIVE；**不是 i3/4GB 真机验收**。
- 本机性能基准（Mock、1s 监控间隔、10s 运行，18 逻辑处理器 / 31.47GB）：
  - 初始化 0.068s；逐个启动 20 房间 2.019s；
  - 空闲工作集 58.04MB，峰值 59.04MB；
  - 20/20 房间 LIVE 且仍在监控；关闭 0.012s。
  - 以上不是低配机器验收结果。
- 受限近似验收（2026-11-16，非低配真机）：
  - 方法：把基准进程 CPU 亲和性限制到 2 个逻辑处理器（严于双核 i3 + 超线程），其余条件不变；
  - 结果：初始化 0.067s、20 房间启动 2.02s、空闲工作集 58.60MB、峰值 59.62MB、20/20 LIVE、关闭 0.014s；
  - 经 `tools\acceptance_low_spec.ps1 -Mode benchmark` 在 2 核亲和性下复跑（22:42）：峰值 59.74MB、
    启动 2.02s、关闭 0.013s、20/20 LIVE，`verdict.passed = true`；
  - 说明：与全核结果基本一致，说明该负载在 2 核内可完成；但内存仍为 31.47GB 环境，
    **不能替代真实 i3 / 4GB 机器验收**，仍需目标机器实测。

## 7. 阻断事项（需授权/条件）

| 事项 | 需要的授权 / 条件 |
| --- | --- |
| 真实 Cookie/Token | 当前公开匿名访问可完成指定房间验证；不主动索取用户凭据 |
| 低配机器验收 | 需要可用的低配测试环境 |

已解除：房间不存在映射（决定保持 `API_MALFORMED`）、录制文件清理（决定不自动删除）、24h 长时运行（已完成）。

## 8. 下一项任务

1. 在目标低配机器（i3 / 4GB）执行验收：
   1. 把项目目录复制到目标机器（含 `tools\acceptance_low_spec.ps1`、`dist\DouyinLiveRecorder\`）；
   2. 安装 Python 3.12 x64（benchmark 模式需要；GUI 冒烟不需要）；
   3. 运行
      `powershell -NoProfile -ExecutionPolicy Bypass -File tools\acceptance_low_spec.ps1 -Mode all`
      首次运行会在 `.venv-acceptance` 中安装 PySide6，并在 `acceptance\` 生成 JSON 报告；
   4. 远程执行可用
      `ssh <user>@<host> "powershell -NoProfile -ExecutionPolicy Bypass -File C:\dlr\tools\acceptance_low_spec.ps1 -Mode benchmark -OutputDir C:\dlr\acceptance"`
      再取回 JSON（GUI 冒烟需桌面会话，SSH 会话下应改用 `-Mode benchmark`）；
   5. 回传 `acceptance\acceptance-*.json`，其中包含 `machine`、`benchmark.criteria`、`gui` 采样与 `verdict`。
   - 脚本判定标准：20 房间全部 `LIVE`；峰值工作集 ≤ 300MB；关闭 ≤ 2s；GUI 采样 ≥ 2 条、峰值 ≤ 300MB、增长 ≤ 30MB、CPU 平均 ≤ 25%（相对整机逻辑核）、正常退出码 0。
   - 录制相关功能的验收：在目标机器用同一打包程序手动添加真实直播间并录制，或用
     `.venv\Scripts\python.exe tools\verify_real.py --record <web_rid> 20`（隔离临时目录，不写入正式数据）。
   - 脚本已在开发机验证三种模式：`info` 正常、`benchmark` 12.86s 通过、`gui` 峰值 100.97MB / 增长 0.06MB / 退出码 0 通过；
     新增 CPU 采样后复测 `gui` 峰值 96.72MB、增长 0.22MB、CPU 平均 0%、退出码 0。
   - 开发机为 Core Ultra 5 125H / 14 核 18 线程 / 31.47GB，**不满足 i3 / 4GB 目标环境**，其数据不得作为低配验收结论。

## 9. 交付状态

- 功能闭环：房间管理、监控、录制、历史、设置、备份/恢复已打通。
- 录制链路：本地媒体、本地直播样例流及用户指定真实抖音流均已验证；真实文件包含视频和音频，历史与 Session 正常收尾。
- 状态映射：`status=2 -> LIVE`、`status=4 -> OFFLINE_CONFIRMED` 已真实验证；无效 ID 不会误判直播。
- Windows 打包：`dist/DouyinLiveRecorder/DouyinLiveRecorder.exe` 已内置 FFmpeg 与真实解析修复；本机启动、GUI 自动录制、历史与退出均已实测。
- 打包程序真实链路：`DouyinLiveRecorder.exe` + 真实房间 `95694179922` 完成 `LIVE/SUCCESS` → 自动录制 23,837,836 字节 → 历史“已完成 22.7 MB” → Session `stopped` → 退出码 0。
- 长时运行：24 小时采样完成，20 房间全程稳定、内存 34–60MB 无增长；期间发现并修复“录制目录被外部删除导致日志刷屏”的缺陷。
- 打包程序已包含磁盘修复（上一版构建 2026-11-17 22:27:46，晚于 `disk.py` 修改时间 22:24:08）；
  实测运行中删除 `data\recordings` 后 12 秒内磁盘告警 0 条、进程存活、关闭退出码 0。
- 候选版本构建时间：2026-11-17 22:52:37（含本阶段全部修复与版本号显示）。
- 2026-11-17 交付前审查新增修复：打包数据目录固定到 EXE 同级、启动失败可见提示（退出码 4）、
  停止后无法确认 FFmpeg 退出时标记 `failed`、WAL 场景安全副本一致性、监控按钮状态以后台为准。
  已重建候选版本 `dist\DouyinLiveRecorder\`（v0.1.0），并完成 GUI 冒烟与干净目录启动验证。
- 交付物范围：运行时只需 `dist\DouyinLiveRecorder\`（含 `_internal`）与 `README.md`、`docs\`；
  工作区中的 `.venv`、`_probe_gui`、`_test_artifacts`、`.screenshots`、`acceptance`、`data\gui_screenshot.png`
  等为开发/验证产物，不应随交付包分发。
- 正式发布（2026-11-18，v0.1.1 缺陷修复）：
  - 缺陷：用户粘贴完整直播间链接时被输入校验拒绝，无法添加房间（0.1.0 只接受纯 `web_rid`）。
  - 修复：添加/编辑直播间支持链接并自动提取 `web_rid`；短链接给出明确提示；非法输入提示具体原因。
  - 构建：`dist\DouyinLiveRecorder\`，构建时间 2026-11-18 01:18:26；启动日志确认 `版本=0.1.1`。
  - 发布包：`release\DouyinLiveRecorder-0.1.1-win64.zip`（78,596,201 字节 / 74.95MB，
    解压约 199.07MB / 172 文件），SHA-256
    `758DA354FA3FA8E0ED2C454065A1FDF724F91F50E60184C354A507D7A5DFA72A`。
  - 验收：完整回归 209 passed；GUI 冒烟 40 条采样、峰值 97.13MB、增长 4.32MB、CPU 平均 0.01%、
    退出码 0；干净环境从 `%TEMP%` 目录启动退出码 0，数据仍写入 EXE 同级 `data\`。
  - 标签：`v0.1.1`；`v0.1.0` 源码、Tag 与发布包均保持不变。
  - 详细记录见 `docs\RELEASE_REPORT_0.1.1.md`。
- 正式发布（2026-11-18，v0.1.0）：
  - 源码基线：Git 仓库已建立，发布标签 `v0.1.0`。
  - 正式构建：`dist\DouyinLiveRecorder\`，构建时间 2026-11-18 00:12:43，与 Git 标签指向源码一致。
  - 发布包：`release\DouyinLiveRecorder-0.1.0-win64.zip`（74.95MB，解压约 199MB），
    校验和见 `release\SHA256SUMS.txt`。
  - 发布验收：完整回归 205 passed；正式构建 GUI 冒烟 8 条采样、峰值 96.57MB、增长 2.56MB、
    CPU 平均 0.72%、退出码 0；发布目录启动/关闭冒烟退出码 0；内置 FFmpeg 正常加载。
- 结论：0.1.1 已完成缺陷修复、构建、验收、打包与版本归档，具备发布条件；
  **低配机器（i3 / 4GB）真机验收仍未执行，不得视为通过**。

## 10. 版本变更记录

### 0.3.1（2026-11-18，直播间链接解析修复）

- 修复 `https://www.douyin.com/follow/live/<数字>` 链接被拒绝的问题；改为按 URL 结构提取房间号，
  支持 `live.douyin.com/<数字>`、`/live/`、`/root/live/`、`/follow/live/`，查询参数只认 `live_web_rid`。
- `anchor_id`、`user_id` 等参数不会被误当作房间号；域名白名单与错误提示区分更明确。
- 验证：新增 17 项链接解析回归，全量 276 passed；用户上报链接经生产解析器实测 status=2（直播中）并有流地址；
  0.3.1 打包版实测添加该链接后显示 `LIVE / SUCCESS`，关闭退出码 0、无残留 FFmpeg。
- 0.3.1 独立验收补充修复：路径房间号改用 ASCII `[0-9]`（原 `\d` 会接受全角/其他语言数字）；
  链接定位允许显式端口（`live.douyin.com:443`），错误提示不再把端口并入域名。

### 0.3.0（2026-11-18，批次 F/G/H）

- 新增 FFmpeg 进程生命周期：Job Object 强杀兜底、进程登记表、身份核验后的遗留进程清理（不误杀）。
- 新增录制清晰度设置（OD/UHD/HD/SD/LD，默认 UHD）与抖音 Cookie 登录配置（DPAPI 加密，不入库不入日志）。
- 修复网络中断后 FFmpeg 以退出码 0 提前结束时被误判为直播结束；输出长时间停滞时改为续录；重试有界。
- 验证：259 passed；真实环境完成崩溃/重启/遗留进程、五档清晰度、Cookie 加密与失效、4 房间 30 分钟、
  单任务强杀隔离、8 秒与长时网络中断、不可达地址有界重试、2 核亲和性基准。
- 未完成：i3/4GB 真机验收。

### 0.1.1（2026-11-18，缺陷修复版本）

- 修复：添加/编辑直播间时可直接粘贴抖音直播间链接，程序自动提取 `web_rid`
  （支持 `live.douyin.com/<数字>`、`www.douyin.com/root/live/<数字>` 及含链接的整段分享文案）。
  原因是 0.1.0 的输入校验只接受纯 ID，用户粘贴链接会在解析前被拒绝（日志为“添加房间参数非法”）。
- 改进：手机端短链接（`v.douyin.com/...`）给出明确提示，说明需在浏览器打开后复制长链接。
- 改进：非法输入直接提示具体原因，不再统一提示“可能 ID 已存在”。
- 改进：添加/编辑对话框标签改为“直播间 ID 或链接”。
- 验证：完整回归 209 passed（新增 4 项链接输入回归）。

### 0.1.0（2026-11-18，正式发布版本）

- 修复：打包程序从不同工作目录启动时会在别处新建空数据库；现固定使用 EXE 所在目录，并支持 `DLR_DATA_DIR` 覆盖。
- 修复：数据目录不可写或数据库损坏导致启动失败时，给出可读提示并返回退出码 4。
- 修复：停止录制后无法确认 FFmpeg 退出时，不再误记为 `completed`，改为 `failed` 并保留原因。
- 修复：备份/恢复在存在 WAL 时改用 SQLite Backup API 复制，避免安全副本缺少已提交数据。
- 改进：开始监控后以后台真实状态刷新“监控中”列，避免 Session 创建失败时界面误报。
- 改进：窗口标题与启动日志显示版本号，便于核对打包版本。
- 验证：完整回归 205 passed；打包 GUI 冒烟退出码 0；干净目录启动退出码 0；20 房间基准 20/20 LIVE。
- 未完成：低配机器（i3 / 4GB）真机验收（缺少目标机器）。
