# 0.2.0 交付报告（技术基线切换版）

- 版本：0.2.0
- 日期：2026-11-18
- 执行：TRAE CN
- 迁移前基线：v0.1.2（`62b6567`）
- 引擎迁移提交：`94326de`

## 1. 本版核心变化

本版按用户决策完成**技术基线切换**：抖音解析主链路改为移植上游
`ihmily/DouyinLiveRecorder` v4.0.7 的 `webcast/room/web/enter` + `a_bogus` 实现，
同时保留本项目已通过 P0 验证的录制服务、数据库、GUI 与单实例能力。

| 维度 | 0.1.2（迁移前） | 0.2.0（迁移后） |
| --- | --- | --- |
| 解析数据源 | 直播页 React Flight（约 1.3 MB） | 直播详情接口 JSON（约 4 KB），页面解析保留为回退 |
| 签名 | 无 | `a_bogus`（上游纯 Python 实现） |
| 实测解析耗时 | 约 1.48 s/房间 | 约 0.39–0.78 s/房间 |
| 风控识别 | 笼统 `API_MALFORMED` | `RISK_CONTROL` + 日志/界面提示 |
| 无效/注销房间 | 可能解析异常 | 正确返回 `status=4`（不误判直播） |
| FFmpeg 参数 | 基础重连参数 | 对齐上游：读写超时、UA、协议白名单、分析/探测缓冲、丢弃损坏包、TS 时间戳修正等 |
| 录制/续录/停止抑制 | 已通过 P0 | 不变 |
| GUI / 数据库 / Session | 已通过 P0 | 不变 |

## 2. 架构

```
GUI（PySide6）
  └─ Application（装配、自动录制开关、手动停止抑制）
      ├─ MonitorService/TaskManager（多房间轮询、Session）
      ├─ RecordingService（状态机、续录、收尾、DB 一致性）
      │    └─ FFmpegProcess（子进程生命周期）
      └─ EngineDouyinParser（主链路）
           ├─ engine/douyin.py：web enter + a_bogus + 流地址选择
           ├─ engine/ab_sign.py：上游签名算法（MIT）
           └─ 回退：RealDouyinParser（React Flight 页面解析）
```

## 3. 质量与测试

- 全量回归：**232 passed**（新增 `tests/test_engine_parser.py` 10 项）。
- 真实解析（离线房间）：房间 646379392926 / 1477297997 均返回正确主播名与 `status=4`，耗时 390–781 ms。
- 无效房间 `web_rid=1`：`status=4`，未误判。
- 监控链路（源码级）：20 s 内 4 次轮询，状态 `OFFLINE_CONFIRMED`，干净退出。
- FFmpeg 新参数：本地 HTTP 流录制 6 s，退出码 0，1,189,100 B，全解码 0 错误。
- 打包版：窗口 `v0.2.0`，界面"开始监控"后 15 s 内显示 `OFFLINE_CONFIRMED / SUCCESS`（真实网络）。
- 数据迁移：`integrity_check=ok`，无孤儿记录，路径改写正确；迁移后正式启动直接可用。
- 数据库 Schema、历史数据、单实例、磁盘保护、备份恢复等既有测试全部保持通过。

## 4. 真实直播验证

**已通过**（2026-11-18 18:44:39–18:45:40）：

| 项目 | 结果 |
| --- | --- |
| 测试房间 | `web_rid=476806339740`（主播"户外发"，实时在播，10 万+观看） |
| 解析 | 引擎返回 `status=2`，取得 FLV 与 HLS 地址（清晰度 UHD） |
| 自动开始录制 | 1 次，无重复启动 |
| 文件增长 | 20 s 6,815,744 B；40 s 12,845,056 B；60 s 19,136,512 B |
| 收尾 | 停止后 `completed`，19,214,916 B |
| DB 一致性 | 记录 `file_size=19,214,916`，与磁盘完全一致；Session 正常结束 |
| 可播放性 | `ffmpeg -v error -i <file> -f null -` 退出码 0，无错误输出 |
| 进程清理 | 关闭后残留 FFmpeg = 0 |

用户自有房间（646379392926 / 1477297997）在本次验证时段内均未开播（持续 15 分钟轮询均为 `status=4`），
因此未能在其房间上复验；上述结果来自同平台的另一个实时开播房间，解析与录制链路相同。
开播后按 `%TEMP%\dlr-p0\watch_and_record.py` 或在 0.2.0 界面点击"开始监控"即可复验。

**打包版真实录制：已通过**（2026-11-18 18:47:31–18:50:56，同一房间 `web_rid=476806339740`）：

| 项目 | 结果 |
| --- | --- |
| 被测程序 | `release\DouyinLiveRecorder-0.2.0-win64\DouyinLiveRecorder.exe`（窗口标题 `v0.2.0`） |
| 启动日志 | `解析器: EngineDouyinParser`、`FFmpeg 路径=...\_internal\ffmpeg\bin\ffmpeg.exe` |
| 数据目录 | `DLR_DATA_DIR=%TEMP%\dlr-p0\pkg_e2e`（隔离库），录制写入迁移后的 `data\recordings` |
| 触发方式 | 界面选中第 3 行 → "开始监控"，自动录制随后启动 1 次（`pid=20052`） |
| 录制时长 | 205 s（3 分 25 秒） |
| 文件增长 | 15,466,496 B（18:48:19）→ 27,787,264 B（18:48:59）→ 54,788,096 B（18:50:28） |
| 收尾 | 关闭主窗口触发 `requested=True` 收尾，`completed`，63,374,236 B |
| 文件信息 | `Duration 00:03:28.31`，H.264 High 1088x1920 22fps，AAC LC 48 kHz 立体声 |
| DB 一致性 | `recordings.id=2 status=completed file_size=63374236 session_id=5`，与磁盘一致 |
| 可播放性 | `ffmpeg -v error -i <file> -f null -` 退出码 0；47 条 `non monotonically increasing dts` 均位于 0.02–0.03 s（起始时间戳重复），非中途损坏 |
| 进程清理 | 关闭后残留 FFmpeg = 0 |

## 5. 构建与复现

```
.venv\Scripts\python.exe -m pip install -e ".[test]"
.venv\Scripts\python.exe -m pytest -q
powershell -ExecutionPolicy Bypass -File build\build.ps1
```

产物：`dist\DouyinLiveRecorder\`，发布副本：`release\DouyinLiveRecorder-0.2.0-win64\`。

发布归档：`release\DouyinLiveRecorder-0.2.0-win64.zip`（77.7 MB，不含 `data` 用户数据）
SHA256：`37F1E714EBACA7AD9E6903EC809ABCDAD563EAE7E1DEA85BE89E6838FC70C7A9`

## 5.1 数据迁移

`release\DouyinLiveRecorder-0.2.0-win64\data` 已从 0.1.2 迁移（源目录保持原样未改动）：

1. 复制 `app.db`、`logs`、`recordings`；
2. 改写 `settings.recording.ffmpeg_path` → 0.2.0 内置 FFmpeg，`recording.output_dir` → 0.2.0 `data\recordings`；
3. 校验 `PRAGMA integrity_check=ok`、房间/录制/Session 计数一致、无孤儿记录；
4. 迁移后启动确认可正常读取房间并解析。

迁移脚本：`%TEMP%\dlr-p0\migrate_012_to_020.py`（默认演练，`--apply` 时若目标已存在则拒绝覆盖）。

## 6. 已知问题与限制

1. 主链路依赖抖音接口及签名规则，上游算法更新时需同步移植；回退链路可独立工作。
2. 默认清晰度 `UHD`（上游下标 1），原画 `OD` 与低画质暂未开放到设置界面。
3. 风控为外部风险：本版能识别并提示，但不能规避。
4. 异常退出可能遗留孤儿 FFmpeg（既有 N2 风险，未在本版处理）。
5. 监控 Session 不自动恢复（既有设计），重启后需手动开始监控。

## 7. 后续路线

1. 设置界面开放清晰度选择（OD/UHD/HD/SD）。
2. Cookie 登录支持（需用户授权与本地凭据保护方案）。
3. 进程跟踪与孤儿 FFmpeg 清理（需引入 PID 持久化或任务对象）。
4. 解析结果的封面/标题展示与房间卡片。
5. 上游引擎版本定期同步机制。
