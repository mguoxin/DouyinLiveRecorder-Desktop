# 技术基线切换记录：迁移到 ihmily/DouyinLiveRecorder 抖音引擎（0.2.0）

- 日期：2026-11-18
- 执行：TRAE CN
- 分支：`feat/upstream-engine`
- 基线提交（迁移前）：`62b6567`（v0.1.2 冻结）
- 新版本：`0.2.0`

## 1. 迁移决策与依据

本项目原解析链路是自研的"直播页 React Flight 数据"解析，在 P0 事故中暴露出两个结构性问题：

1. 单点依赖页面结构，抖音调整页面/风控策略后无第二数据源；
2. 页面体积约 1.3 MB，解析耗时约 1.48 s/房间，且风控页只能笼统归为 `API_MALFORMED`。

上游 `ihmily/DouyinLiveRecorder` v4.0.7 的抖音链路经过大规模长期实际使用，采用
`webcast/room/web/enter` 接口 + `a_bogus` 签名，返回约 4 KB JSON，具备明确的失败语义。
因此确定以**上游抖音引擎为解析主链路**，保留本项目已通过 P0 验证的录制服务、数据库与 GUI 层。

本次迁移不复制上游多平台代码，不改变本项目的产品结构（Qt 桌面端 + SQLite + 单实例）。

## 2. 迁移范围（逐项）

| 能力 | 处理方式 | 说明 |
| --- | --- | --- |
| 抖音房间解析 | **移植上游** | `webcast/room/web/enter` + `a_bogus`（`src/dlrecorder/douyin/engine/`） |
| a_bogus 签名 | **原样移植** | 纯 Python，无 Node/JS 运行时依赖 |
| 流地址选择 | **移植并简化** | 清晰度映射 OD/UHD/HD/SD/LD，HEAD 探测失败自动降级 |
| 原画地址注入 | **移植** | `live_core_sdk_data` 中的 `origin`（or4）注入地址字典首位 |
| FFmpeg 参数 | **对齐上游** | `rw_timeout`、`user_agent`、`protocol_whitelist`、`thread_queue_size`、`analyzeduration`、`probesize`、`fflags +discardcorrupt`、`correct_ts_overflow`、`avoid_negative_ts`、`-sn -dn`、`max_muxing_queue_size` |
| 页面解析（React Flight） | **保留为回退** | 主链路风控/结构异常时作为第二数据源，拿到确定结果才采用 |
| 录制服务/续录/停止抑制 | **保留不动** | 已通过 P0 的 222 项测试与真实录制验证 |
| 数据库、历史、Session | **保留不动** | Schema V2 与一致性测试继续有效 |
| GUI / 多房间监控 / 单实例 | **保留不动** | 产品层不受解析迁移影响 |
| 上游多平台（40+） | **不引入** | 产品定位为抖音录播 |
| 上游代理/账号/Cookie 配置 | **不引入** | 仅使用上游代码中的公开匿名 ttwid，不读取用户凭据 |

## 3. 刻意未采用的上游实现

| 上游行为 | 本项目处理 | 原因 |
| --- | --- | --- |
| `-reconnect_streamed 1` / `-reconnect_at_eof` | 不启用 | 本项目已实测：直播结束（EOF）后 FFmpeg 会持续重连而不退出。改为进程退出后由录制服务重新解析，确认仍在直播才续录（P0 N1 修复）。 |
| 全局 `print` 错误输出 | 改为分类异常 | 空响应/非 JSON → `RISK_CONTROL`；无房间数据 → `API_MALFORMED`；直播中无流地址 → `StreamInfoMissingError`；传输失败 → `NETWORK_ERROR`。避免"解析成功但无流"的假阳性。 |
| 无超时/无分类的请求 | 显式 10s 超时 + 异常分类 | 与现有监控线程模型兼容，单房间失败不影响其他房间。 |
| 对全部清晰度探测 HEAD | 首选成功即停 | 减少请求量；全部失败仍返回首选地址，由录制阶段如实报错。 |

## 4. 代码结构

```
src/dlrecorder/douyin/engine/
  __init__.py      # 移植说明
  ab_sign.py       # 上游 a_bogus 签名（MIT，未改逻辑）
  douyin.py        # web enter 请求、原画注入、流地址选择、错误类型
  parser.py        # EngineDouyinParser：适配本项目解析器接口 + 回退链
```

`Application._build_parser()` 现在返回 `EngineDouyinParser(fallback=RealDouyinParser(...))`。

## 5. 许可证与合规

- 上游许可：MIT License，Copyright (c) 2025 Hmily。
- 已在 `THIRD_PARTY_NOTICES.txt` 记录移植来源、范围与 MIT 全文，并在源文件头部注明出处。
- 新增依赖：`httpx[http2]`（BSD-3-Clause）、`httpcore`、`h2`（MIT），已列入第三方清单。

## 6. 验证记录

| 类别 | 结果 | 证据 |
| --- | --- | --- |
| 单元/回归测试 | **232 passed**（新增 10 项引擎适配测试） | `pytest -q` |
| 真实房间解析（离线） | **通过**：390–781 ms/房间，正确返回主播名与 `status=4` | `%TEMP%\dlr-p0\probe_engine.py` |
| 无效房间（web_rid=1） | **通过**：`status=4`（账号已注销），未误判为直播 | 同上 |
| 监控链路（源码） | **通过**：20 s 内 4 次轮询，状态 `OFFLINE_CONFIRMED`，应用干净退出 | `%TEMP%\dlr-p0\smoke_engine_app.py` |
| FFmpeg 新参数 | **通过**：本地 HTTP 流录制 6 s，退出码 0，1,189,100 B，全解码 0 错误 | `%TEMP%\dlr-p0\check_ffmpeg_args2.py` |
| 打包版启动 | **通过**：窗口标题 `v0.2.0`，日志"解析器=EngineDouyinParser"，httpx 已打包（无导入错误） | `dist\DouyinLiveRecorder` 实测 |
| 打包版界面解析 | **通过**：点击"开始监控"后 15 s 内，界面显示 `确认状态=OFFLINE_CONFIRMED`、`最近解析=SUCCESS`（真实网络请求） | 截图级 UIA 读取 |
| 数据迁移演练 | **通过**：`integrity_check=ok`，房间/录制/Session 计数一致，无孤儿记录，路径已改写为新包 | `%TEMP%\dlr-p0\migtest2` |
| 迁移后正式启动 | **通过**：0.2.0 发布目录直接读取迁移数据，加载房间 2 并完成真实解析 | `release\DouyinLiveRecorder-0.2.0-win64\data\logs\app.log` |
| 真实抖音直播录制 | **通过**（2026-11-18 18:44:39–18:45:40，`web_rid=476806339740`，主播"户外发"） | 引擎解析 `status=2` 并取得 FLV；自动开始录制；文件 20/40/60 s 分别为 6,815,744 / 12,845,056 / 19,136,512 B；停止后 `completed` 19,214,916 B，与磁盘一致；全解码 `exit=0`；FFmpeg 残留 0；Session `stopped` |
| 打包版真实录制 | **通过**（2026-11-18 18:47:31–18:50:56，同一房间） | `release\DouyinLiveRecorder-0.2.0-win64\DouyinLiveRecorder.exe`（日志 `解析器: EngineDouyinParser`）；自动开始 1 次 `pid=20052`；205 s 后收尾 `completed` 63,374,236 B；`Duration 00:03:28.31`、H.264 1088x1920 + AAC；DB `file_size=63374236 session_id=5` 与磁盘一致；全解码退出码 0（47 条 DTS 告警仅位于 0.02–0.03 s 起始处）；FFmpeg 残留 0 |

## 7. 已知限制与后续路线

1. 主链路依赖抖音接口与签名规则；上游更新签名算法时需要同步移植（回退链路可独立工作）。
2. 清晰度默认 `UHD`（下标 1）。如需原画/低画质，后续在设置中开放 `quality` 选项。
3. 多房间并发轮询为单线程顺序执行；实测 0.4–0.8 s/房间，20 房间仍在 8–16 s 内完成，满足 30 s 默认间隔。
4. 风控仍为外部风险：现在能准确识别并提示（`RISK_CONTROL` + UI"风控验证"），Cookie 登录支持另行立项。
