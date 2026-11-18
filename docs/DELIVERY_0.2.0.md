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

- 本次验证时段内，已配置房间均未开播，**真实抖音直播录制标记为「未执行（外部条件）」**，不作为通过结论。
- 已完成的部分：解析（真实网络）、监控状态流转、FFmpeg 参数有效性（本地 HTTP 流）。
- 开播后的复验流程：`%TEMP%\dlr-p0\watch_and_record.py`（30 分钟监听）或直接在 0.2.0 界面点击"开始监控"，
  记录项包括：开始录制时间、文件增长采样、停止后 DB 记录与磁盘一致性、全解码结果、FFmpeg 进程残留数。

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
