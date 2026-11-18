# DouyinLiveRecorder-Desktop 0.1.1 上游技术溯源与架构符合性审查报告

- 审查日期：2026-11-18
- 审查对象：DouyinLiveRecorder-Desktop **0.1.1**（Git Tag `v0.1.1` = `abcc7e0`）
- 审查性质：**只读审查**。未修改 0.1.1 源码、发布包、Git Tag、历史提交与用户数据；未引入任何新依赖。
- 上游基准：`https://github.com/ihmily/DouyinLiveRecorder`
  - 快照 commit：`add187f8d8c7ff7d231fcbee45cbb4f1ed247d3a`（2025-11-03，`feat: basic support for running with uv (#1265)`）
  - 最新正式 Release：`v4.0.7`（2025-10-24）
  - 许可：MIT（`LICENSE`，Copyright (c) 2025 Hmily）
  - 源码获取方式：下载仓库归档到独立临时目录 `%TEMP%\dlr-upstream`，未写入项目工作区
- 本地验证：`.venv\Scripts\python.exe -m pytest -q` → **209 passed**（离线 30.90s，2026-11-18 本次审查复跑）

---

## 0. 直接结论（先回答问题）

> **我们现在开发的 DouyinLiveRecorder-Desktop，究竟是在成熟开源录制项目基础上构建桌面产品，还是已经逐渐变成了一套与上游技术体系脱节的独立实现？**

**结论：是后者，而且不是“逐渐脱节”，而是从第一行业务代码起就没有发生过技术继承。**

证据（均可复核）：

1. 当前仓库 **没有任何上游代码**：无 `vendor/`、无上游包、无源码片段。唯一运行时依赖是 `PySide6-Essentials`（[pyproject.toml](../pyproject.toml)），上游使用的 `requests` / `httpx[http2]` / `PyExecJS` / `pycryptodome` / `loguru` 一个都没有引入。
2. **没有上游远程与同步痕迹**：`git remote -v` 为空；`git log` 仅 4 个提交，全部为 2026-11-18；`docs/` 与 `THIRD_PARTY_NOTICES.txt` 中检索 `ihmily` / `上游` 均无命中。
3. **核心数据源不同**：上游通过 `live.douyin.com/webcast/room/web/enter/` 接口 + `a_bogus` 签名（[spider.py:68-141](file:///C:/Users/kglpl/AppData/Local/Temp/dlr-upstream/DouyinLiveRecorder-main/src/spider.py#L68-L141)、`src/ab_sign.py`），本地通过公开直播页 React Flight 内嵌数据（[parser.py:90-133](file:///c:/Users/kglpl/Desktop/DouyinLiveRecorder-Desktop/src/dlrecorder/douyin/parser.py#L90-L133)），**不是同一套抓取逻辑**。
4. **录制命令不同量级**：上游 FFmpeg 命令含 `-rw_timeout`、`-user_agent`、`-reconnect_streamed`、`-reconnect_at_eof`、`-reconnect_delay_max 60`、分段与转码（[main.py:1175-1204](file:///C:/Users/kglpl/AppData/Local/Temp/dlr-upstream/DouyinLiveRecorder-main/main.py#L1175-L1204)）；本地只有 8 个参数、`-c copy` 直封装 TS（[ffmpeg.py:104-127](file:///c:/Users/kglpl/Desktop/DouyinLiveRecorder-Desktop/src/dlrecorder/infrastructure/ffmpeg.py#L104-L127)）。
5. **这是有意决策，不是失控漂移**：2026-11-14 第二阶段技术审查后形成的工程约束明确写着“优先自研核心平台解析逻辑，如需复用第三方库需添加适配层隔离”，并限定“不得擅自扩展为多平台直播工具”。

因此更准确的说法是：

- **字面技术路线（“以上游为核心技术基础”）未被执行**：代码复用率 **0%**，没有封装、没有修改继承、没有子模块。
- **工程经验层面有有限继承**：`status=2/4` 的状态语义、TS 直封装、`q` 优雅收尾、循环值守、磁盘阈值等做法与上游一致（属通用做法或协议事实，非代码迁移）。
- **代价已经出现**：上游在 2025-10-24 专门修复了“抖音风控无法获取数据”，本地恰好缺少签名与第二数据源回退；上游的断线重连与流地址续录能力，本地完全没有。

---

## 1. 上游项目技术结构概览

### 1.1 形态与定位

上游是**配置驱动的命令行/容器值守工具**，不是桌面应用。`index.html` 只是 FLV/M3U8 播放预览页；分发形态是 ZIP + `config/` 目录 + `DouyinLiveRecorder.exe`。无 GUI、无数据库。

- 入口：[main.py](file:///C:/Users/kglpl/AppData/Local/Temp/dlr-upstream/DouyinLiveRecorder-main/main.py)（2,155 行）
- 配置：[config/config.ini](file:///C:/Users/kglpl/AppData/Local/Temp/dlr-upstream/DouyinLiveRecorder-main/config/config.ini)（5,329 字节，约 60 项）、`config/URL_config.ini`（一行一个直播间）
- 运行方式：`python main.py` / `uv run main.py` / Docker

> 说明：下文上游代码位置的超链接指向本次审查的临时快照目录（`%TEMP%\dlr-upstream`），该目录可能被后续清理；长期引用请以 commit `add187f8` 为准，行号在同一提交内有效。

### 1.2 核心模块

| 文件 | 行数量级 | 职责 |
| --- | --- | --- |
| `main.py` | 2,155 行 | 读取配置、信号量与线程模型、`start_record()` 主循环、FFmpeg 命令拼装、分段、转码、字幕、推送、自定义脚本、备份配置 |
| `src/spider.py` | 3,395 行 | **79 个平台级函数**（`get_douyin_web_stream_data`、`get_tiktok_stream_data`、`get_kuaishou_stream_data` … `get_picarto_stream_url`），每个平台独立请求与解析 |
| `src/stream.py` | 446 行 | 按平台把“原始数据”转成 `record_url`，含清晰度映射 `QUALITY_MAPPING`（`OD/BD/UHD/HD/SD/LD`）与地址可用性探测 |
| `src/room.py` | 150 行 | 抖音短链/主页链接解析（`get_sec_user_id`、`get_unique_id`、`get_live_room_id`），X-Bogus 生成 |
| `src/ab_sign.py` + `src/javascript/*.js` | 13.7KB + 约 160KB JS | `a_bogus` 签名、`x-bogus.js`、`crypto-js`、各平台 JS 解密（依赖 PyExecJS/Node） |
| `src/proxy.py`、`src/http_clients/*` | 小 | 代理处理；httpx 同步/异步封装 |
| `src/initializer.py`、`ffmpeg_install.py` | 8.2KB / 8.6KB | 运行时检查并安装 Node、FFmpeg |
| `msg_push.py` | 11.2KB | 微信/钉钉/TG/邮箱/Bark/ntfy/pushplus 推送 |
| `Dockerfile`、`docker-compose.yaml`、`StopRecording.vbs` | 小 | 容器与 Windows 停止脚本 |

### 1.3 抖音链路的实际行为

1. **获取房间数据**：优先 `web/enter` 接口（带 `a_bogus`）→ 失败回退到 `reflow/info`（app 参数）→ 再失败回退解析 `live.douyin.com` 页面 HTML（[spider.py:230-282](file:///C:/Users/kglpl/AppData/Local/Temp/dlr-upstream/DouyinLiveRecorder-main/src/spider.py#L230-L282)）。**三层回退**。
2. **状态判定**：`status == 2` 视为直播中；`4` 视为未开播；直播中若无 `stream_url` 明确抛错（VR/特殊玩法不支持 PC 端）。
3. **流地址选择**：`get_douyin_stream_url()` 按清晰度取 FLV/HLS，**先探测地址可用性，不可用自动换一档**（[stream.py:41-78](file:///C:/Users/kglpl/AppData/Local/Temp/dlr-upstream/DouyinLiveRecorder-main/src/stream.py#L41-L78)）。
4. **录制**：每个直播间一个线程 + 全局 `semaphore`（默认 3 并发）；`ffmpeg` 带重连参数；退出码 0 后按配置转 MP4 / 分段 / 生成字幕 / 执行脚本；`q` 写入 stdin 优雅收尾（[main.py:436-449](file:///C:/Users/kglpl/AppData/Local/Temp/dlr-upstream/DouyinLiveRecorder-main/main.py#L436-L449)）。

---

## 2. 当前项目与上游模块对应关系表

> 关系判定标准：
> **直接复用** = 复制或引入上游源码；**封装调用** = 依赖上游包/模块并包一层；
> **修改继承** = 保留上游结构改写；**独立重写** = 目标相同但代码独立；
> **未采用** = 上游有能力、本地没有；**本地新增** = 上游没有、本地有。

| 能力 | 上游实现（commit `add187f8`） | 当前 0.1.1 实现 | 关系 |
| --- | --- | --- | --- |
| 链接识别（长链/短链/主页） | `room.py:52 get_sec_user_id`、`:78 get_unique_id`、`:109 get_live_room_id` | [models.py:26 `_extract_web_rid`](file:///c:/Users/kglpl/Desktop/DouyinLiveRecorder-Desktop/src/dlrecorder/domain/models.py#L26-L53)、`validate_web_rid` | 独立重写（仅长链；短链只提示） |
| 房间信息与开播状态 | `spider.py:68/145/230` 三路回退 + `ab_sign` | [parser.py:153 `RealDouyinParser`](file:///c:/Users/kglpl/Desktop/DouyinLiveRecorder-Desktop/src/dlrecorder/douyin/parser.py#L153-L237) + [parser.py:90 `_extract_page_room`](file:///c:/Users/kglpl/Desktop/DouyinLiveRecorder-Desktop/src/dlrecorder/douyin/parser.py#L90-L133) | 独立重写（单一数据源） |
| 状态语义（2=LIVE / 4=OFFLINE） | `spider.py:110`、`stream.py:51` 等硬编码 | [states.py:30 `KNOWN_STATUS_MAP`](file:///c:/Users/kglpl/Desktop/DouyinLiveRecorder-Desktop/src/dlrecorder/domain/states.py#L30-L33) | 语义一致，代码独立 |
| 流地址解析与清晰度 | `stream.py:41 get_douyin_stream_url`（含可用性探测） | [parser.py:64 `_extract_room_stream_url`](file:///c:/Users/kglpl/Desktop/DouyinLiveRecorder-Desktop/src/dlrecorder/douyin/parser.py#L64-L82) | 独立重写（无清晰度、无探测） |
| 签名/风控 | `src/ab_sign.py`、`src/javascript/x-bogus.js`、PyExecJS | 无 | 未采用 |
| 监控任务调度 | `main.py:545 start_record` + 线程 + semaphore | [task_manager.py:79 `MonitorWorker`](file:///c:/Users/kglpl/Desktop/DouyinLiveRecorder-Desktop/src/dlrecorder/core/task_manager.py#L79-L140)、`:216 _monitor_loop` | 独立重写（asyncio + 单事件循环） |
| 录制进程管理 | `main.py:420 check_subprocess` | [recording_service.py:75 `RecordingService`](file:///c:/Users/kglpl/Desktop/DouyinLiveRecorder-Desktop/src/dlrecorder/application/recording_service.py#L75-L137)、[ffmpeg.py:130 `FFmpegProcess`](file:///c:/Users/kglpl/Desktop/DouyinLiveRecorder-Desktop/src/dlrecorder/infrastructure/ffmpeg.py#L130-L262) | 独立重写（管理线程 + 命令队列） |
| FFmpeg 命令 | `main.py:1175-1204`（含 UA/headers/proxy/reconnect） | [ffmpeg.py:104 `build_record_command`](file:///c:/Users/kglpl/Desktop/DouyinLiveRecorder-Desktop/src/dlrecorder/infrastructure/ffmpeg.py#L104-L127) | 独立重写（精简，缺重连） |
| 优雅停止 | `main.py:441-447` 写 `q` | [ffmpeg.py:185 `stop`](file:///c:/Users/kglpl/Desktop/DouyinLiveRecorder-Desktop/src/dlrecorder/infrastructure/ffmpeg.py#L185-L262)（q → terminate → kill） | 思路同源，代码独立 |
| 文件命名/目录规则 | `main.py:494 clean_name` + 配置项 | [recording_service.py:516 `_allocate_output_path`](file:///c:/Users/kglpl/Desktop/DouyinLiveRecorder-Desktop/src/dlrecorder/application/recording_service.py#L516-L524) | 独立重写（`名称_ID_时间_随机.ts`） |
| 分段录制 | `main.py:1254-1544`、`segment_video()` | 无 | 未采用 |
| 转 MP4 / H.264 / 字幕 / 自定义脚本 | `main.py:219/254/273/356` | 无 | 未采用 |
| 失败重试/循环 | `main.py:555/577/664`、`error_count` | [task_manager.py:231 `while not stop_evt.is_set()`](file:///c:/Users/kglpl/Desktop/DouyinLiveRecorder-Desktop/src/dlrecorder/core/task_manager.py#L216-L290) | 部分对应（按间隔重查，非录制级重试） |
| 断线重连 | FFmpeg `-reconnect_streamed/-reconnect_at_eof` | 无 | 未采用 |
| 配置管理 | `config/config.ini` + `URL_config.ini` | SQLite `settings` 表 + [settings_service.py:94](file:///c:/Users/kglpl/Desktop/DouyinLiveRecorder-Desktop/src/dlrecorder/application/settings_service.py#L94-L166) | 独立重写 |
| 持久化与历史 | 无数据库 | [db.py:44 `Database`](file:///c:/Users/kglpl/Desktop/DouyinLiveRecorder-Desktop/src/dlrecorder/infrastructure/db.py#L44-L58)（Schema V2、WAL、迁移校验） | 本地新增 |
| 桌面 GUI | 无 | `presentation/main_window.py`、`widgets.py` | 本地新增 |
| 单实例 | `StopRecording.vbs`（停止脚本，非单实例） | [single_instance.py:52](file:///c:/Users/kglpl/Desktop/DouyinLiveRecorder-Desktop/src/dlrecorder/infrastructure/single_instance.py#L52-L107) 命名互斥量 | 本地新增 |
| 备份/恢复 | `main.py:1648 backup_file`（备份配置文件） | [backup.py:87/107](file:///c:/Users/kglpl/Desktop/DouyinLiveRecorder-Desktop/src/dlrecorder/infrastructure/backup.py#L87-L135)（SQLite Backup API） | 独立实现，目标不同 |
| 推送通知 | `msg_push.py`（7 种渠道） | 无 | 未采用 |
| 代理 | `src/proxy.py` + 按平台代理列表 | 无 | 未采用 |
| 平台覆盖 | 50+（README 清单） | 仅抖音 | 未采用（有意的范围收缩） |
| 依赖安装 | `ffmpeg_install.py`、`initializer.py`（安装 Node） | 内置 `tools/ffmpeg`，[ffmpeg.py:50 `find_local_ffmpeg`](file:///c:/Users/kglpl/Desktop/DouyinLiveRecorder-Desktop/src/dlrecorder/infrastructure/ffmpeg.py#L50-L66) | 独立实现 |

### 2.1 继承量化

| 类别 | 数量 | 说明 |
| --- | --- | --- |
| 直接复用上游代码 | **0** | 无复制、无 vendor、无子模块、文件头无上游版权 |
| 封装调用上游 | **0** | 依赖清单仅 PySide6；未安装上游包 |
| 修改继承 | **0** | 无同构代码结构 |
| 独立重写 | 9 项 | 链接、解析、监控、录制、FFmpeg 参数、文件命名、配置、下载/发现 FFmpeg、停止流程 |
| 完全未采用 | 8 项 | 签名、短链、清晰度、分段、转码/字幕/脚本、推送、代理、多平台 |
| 本地新增（上游无） | 6 项 | 桌面 GUI、SQLite 持久化与迁移、单实例、备份恢复、磁盘保护、Session 历史 |

> 结论：**在本报告定义的“技术继承”意义上，当前项目与上游的代码同源率为 0。**

---

## 3. 核心能力继承及重新实现情况

### 3.1 直播间信息获取

- 上游：接口优先（`web/enter` + `a_bogus`）→ app 回退 → 页面回退；**抗改版能力强，但依赖 JS 签名与 Node**。
- 本地：只读公开页 `https://www.douyin.com/root/live/<rid>`，从 `self.__pace_f.push([1,"..."])` 记录中还原 React Flight 文本，再用 `json.JSONDecoder().raw_decode` 逐条解析（[parser.py:85-133](file:///c:/Users/kglpl/Desktop/DouyinLiveRecorder-Desktop/src/dlrecorder/douyin/parser.py#L85-L133)）。
- 评价：本地方案**没有签名依赖、无需 Node、无凭据**，隐私与部署复杂度更优；但**只有一条数据通路**，抗改版/抗风控能力明显弱于上游。

### 3.2 直播状态监控

- 本地每个房间一个 asyncio 任务，`interval` 秒轮询；请求经 `HttpExecutor` 统一排队（容量 8），队列满只提示一次并继续重试（[task_manager.py:228-268](file:///c:/Users/kglpl/Desktop/DouyinLiveRecorder-Desktop/src/dlrecorder/core/task_manager.py#L228-L268)）。
- 状态机严格区分“已确认状态”与“本次观测”，解析失败**不覆盖**上次确认状态（[states.py:39-72](file:///c:/Users/kglpl/Desktop/DouyinLiveRecorder-Desktop/src/dlrecorder/domain/states.py#L39-L72)）。这一点优于上游的“本轮取不到就等待”。

### 3.3 直播流解析

- 本地从房间对象中按 `FULL_HD1/HD1/SD1/SD2` 取第一条非空 FLV/HLS（[parser.py:50-61](file:///c:/Users/kglpl/Desktop/DouyinLiveRecorder-Desktop/src/dlrecorder/douyin/parser.py#L50-L61)），**不校验地址是否可播放**。
- 上游会探测可用性并降档重试（`stream.py:65-69`）。这是**可感知的行为差异**：本地可能拿到已失效的高码率地址，表现为“开始录制即失败”或“立刻停止”。

### 3.4 录制任务管理

- 本地为“管理线程 + 命令队列”（`RecordingService._run/_handle_start/_poll_active/_finalize`），GUI 不阻塞；每房间独立 `_ActiveRecording`，失败/停止/磁盘不足统一 `_finalize`。
- 上游为“每房间一线程 + 全局信号量”，并在同一线程内阻塞等待 FFmpeg。
- 评价：本地的线程模型更适合桌面应用，但**缺少录制级监督（supervisor）**：FFmpeg 退出后即收尾，不会在同一个录制会话内重新拉流续录。

### 3.5 录制文件处理

- 本地：固定 `-c copy -f mpegts`，文件名 `显示名_web_rid_时间_随机.ts`；不做分段、不做转码、不做清理（符合既定策略“不自动删除录制文件”）。
- 上游：支持 ts/mkv/flv/mp4/音频，分段 1800s、转 mp4/h264、字幕、脚本、按标题/作者/时间建目录。
- 风险：**长时间单文件**在异常中断时完整性差；TS 兼容性虽好但用户需自行转码。

### 3.6 网络异常与断线恢复

- 本地：传输错误细分为超时/DNS/TLS/连接/协议（[http_client.py:26-101](file:///c:/Users/kglpl/Desktop/DouyinLiveRecorder-Desktop/src/dlrecorder/infrastructure/http_client.py#L26-L101)），解析失败映射 `NETWORK_ERROR` 且不改变确认状态；录制中断只会标记 `failed`/`interrupted`。
- 上游：FFmpeg 层 `-reconnect_streamed -reconnect_at_eof -reconnect_delay_max 60`，外加外层循环重新探测。
- **这是本次审查发现的最高优先级能力缺口**（详见 4.1 F1）。

### 3.7 平台接口兼容性

- 本地只针对抖音公开页一种结构，无签名、无 Cookie、无代理、无第二数据源；页面结构变化时统一落入 `API_MALFORMED`（[parser.py:230-232](file:///c:/Users/kglpl/Desktop/DouyinLiveRecorder-Desktop/src/dlrecorder/douyin/parser.py#L230-L232)）。
- 行为上是安全的（不会误开录制），但**可用性是单点的**。

---

## 4. 功能缺失与行为差异清单

### 4.1 高优先级（影响录制可用性）

| 编号 | 差异 | 上游证据 | 本地现状 | 影响 |
| --- | --- | --- | --- | --- |
| **F1** | 无断线重连/无录制中流地址刷新 | `main.py:1190-1191`（`-reconnect_streamed/-reconnect_at_eof`） | `build_record_command` 无任何 reconnect 参数（`ffmpeg.py:104-127`）；`RecordingService` 只在开始录制时接收一次 `stream_url`（`recording_service.py:267`） | 抖音流地址通常带时效；长时段录制中断后只能等下个监控周期（默认间隔）重新建文件，中间丢片；这与项目自身经验记录“需在架构设计阶段实现录制过程中的流地址刷新逻辑”不一致 |
| **F2** | 流地址可用性不校验 | `stream.py:65-69` 探测失败自动降档 | `_extract_room_stream_url` 取第一个非空值 | 可能拿到失效的高码率地址，表现为“开始即失败” |
| **F3** | 无第二数据源回退 | `spider.py:68/145/230` 三层回退 | 仅公开页 React Flight | 页面改版或风控页 → 全部房间 `API_MALFORMED`，无降级路径 |

### 4.2 中优先级（功能覆盖）

| 编号 | 差异 | 上游证据 | 本地现状 | 影响 |
| --- | --- | --- | --- | --- |
| F4 | 不支持短链/主页/分享文案中的短链 | `room.py:52-105` | 仅 `live.douyin.com/<数字>`、`www.douyin.com/root/live/<数字>`；`v.douyin.com` 只给提示（`models.py:31-35`） | 用户多一步操作，但不阻断 |
| F5 | 无清晰度选择 | `stream.py:26 QUALITY_MAPPING` | 取第一个可用地址 | 无法指定原画/高清，弱网下体验差 |
| F6 | 无 Cookie/登录态 | `config.ini [Cookie]` | 匿名会话（`app.py:62-65`） | 隐私更优；但对需登录/强风控房间无手段 |
| F7 | 无分段录制 | `main.py:1254-1544` | 单文件 | 长时间录制文件过大，异常损失大 |
| F8 | 无转码/字幕/自定义脚本 | `main.py:219/254/273/356` | 无 | 需用户自行处理 |
| F9 | 无状态推送 | `msg_push.py` | 无 | 必须开着界面才知道开播/下播 |
| F10 | 无代理 | `src/proxy.py` | 无 | 当前仅抖音，影响有限 |
| F11 | 录制失败无退避/升级重试 | `main.py:555/577/664` | `_poll_active` 发现退出即 `_finalize`（`recording_service.py:375-433`） | 自动录制会在下一监控周期重启（`app.py:139-157`），手动录制不会；无退避，可能频繁起停 |

### 4.3 行为差异（非缺陷，需明确说明）

| 项 | 上游 | 本地 | 说明 |
| --- | --- | --- | --- |
| 房间不存在 | 解析异常/空数据，行为分散 | 统一 `API_MALFORMED`，不映射 `ROOM_NOT_EXIST` | 已在 0.1.0 决策中确认，属**有意的保守策略** |
| 状态过期 | 无显式概念 | `expire_threshold_s=600`（`states.py:53-60`） | 本地更严谨 |
| 并发模型 | 线程 + 信号量（默认 3） | 单事件循环 + 执行器容量 8 | 本地更适合 GUI，但对目标站点更“激进” |
| 文件保留 | 可配置删除原文件 | 永不删除 | 本地符合既定数据安全策略 |
| 数据形态 | INI 文件 | SQLite Schema V2 + 迁移 + 备份 | 本地显著更工程化 |

---

## 5. 当前架构的合理性评价

### 5.1 分层与职责

实际分层比文档描述的“四层”多一层 `core/`：

```
presentation/   PySide6 界面、对话框、日志桥接
application/    装配（app.py）、监控协调（monitor_service）、录制（recording_service）、设置
core/           任务与事件循环（task_manager.py，QThread + asyncio）
domain/         状态模型（states.py）、房间与校验（models.py）、录制状态（recording.py）
infrastructure/ db、http_client/http_executor、ffmpeg、disk、backup、single_instance、logging
douyin/         抖音解析器（parser.py）
```

评价：

- **合理**：Domain 不依赖 Qt/SQL（`states.py` 仅用标准库）；录制与监控解耦；`douyin/` 与通用逻辑隔离，便于替换数据源。这是上游（单文件 2155 行 + 3395 行 spider）所不具备的可维护性。
- **合理的取舍**：自研解析换来“无 Node、无签名、无凭据、打包体积小”，与低配 Windows 目标一致。
- **需要修正的地方**（不是重构，只是定位问题）：
  1. `src/dlrecorder/douyin/` 不在四层之中，文档应把它定义为“可替换的平台适配层”，否则后续容易继续堆平台代码。
  2. 存在**仅测试使用的遗留路径**：`extract_stream_url()`（`parser.py:136-150`）与 `classify_response()`（`states.py:75-107`）是旧 `web/enter` JSON 时代的产物，生产解析器已不使用（经全仓检索确认，仅 `tests/test_parser.py` 引用）。留着会误导后续维护者以为存在第二条解析通路。
  3. 录制服务接口天然不支持“换地址续录”：`start_recording(room_id, stream_url)` 一次性传参，没有 `update_stream_url` 或 supervisor 概念。

### 5.2 是否会因重复实现上游功能而增加维护成本

- **重复实现的部分很少**：真正与上游重叠的只有“抖音状态+流地址解析”这一块（约 150 行核心逻辑，`parser.py:50-237`）。
- **不重叠的部分**（GUI、数据库、迁移、备份、单实例、磁盘保护）上游根本没有，谈不上重复。
- 因此**维护成本主要不是“重复造轮子”，而是“单点解析器需要自己跟抖音改版”**。这是一个明确、可量化的成本，而不是架构浪费。

---

## 6. 上游更新同步能力评价

| 能力 | 现状 | 证据 |
| --- | --- | --- |
| 追踪上游更新 | **不具备** | `git remote -v` 为空；无 submodule；无版本 pin 文件；文档无上游链接 |
| 识别上游关键修复 | **不具备机制**（只能人工盯 release） | 上游 `v4.0.7` 修复“抖音风控无法获取数据”，本地无任何记录 |
| 合并上游改进 | **不可能**（无共享代码） | 无 vendor/upstream 目录 |
| 避免冲突 | **天然无冲突** | 零代码共享 |

评价：**“无法同步”与“没有冲突”是同一件事的两面。** 当前选择避免了上游破坏性变更（上游频繁改 `spider.py`，且强依赖 JS 签名），但也意味着：

- 上游对抗风控的成果无法吸收；
- 上游对 FFmpeg 参数（重连、超时、缓冲区）的调优经验无法自动获得；
- 一旦抖音改版，本地只能自己重新逆向公开页结构。

**建议的同步方式不是代码合并，而是“情报同步”**：定期（如每季度或每次上游 Release）记录上游 `Releases` 与关键 commit 摘要，形成对照清单，用于指导本地解析器与录制参数调整。

---

## 7. 维护成本与技术风险评估

| 编号 | 风险 | 等级 | 依据 | 后果 |
| --- | --- | --- | --- | --- |
| R1 | 解析器单点依赖 React Flight 页面结构 | **高** | `parser.py:85-133`，无回退 | 抖音改版/风控页 → 所有房间 `API_MALFORMED`，功能整体不可用 |
| R2 | 无断线重连与流地址刷新 | **高** | `ffmpeg.py:104-127`；`recording_service.py:267` | 长时段录制中断、丢片；与项目自身经验条目冲突 |
| R3 | 无清晰度探测 | 中 | `parser.py:50-61` | 偶发“开始录制即失败” |
| R4 | 文档定位与事实不符 | 中 | 本次审查结论 | 后续维护者以为存在上游继承，误判升级路径与许可义务 |
| R5 | 命名与上游相同 | 中 | 仓库名 `DouyinLiveRecorder-Desktop` | 易被误认为上游衍生版；当前未复用代码故无 MIT 义务，但应在“关于/说明”中澄清 |
| R6 | 遗留解析路径 | 低 | `parser.py:136`、`states.py:75` 仅测试使用 | 维护者误判架构，增加无效排查 |
| R7 | 无签名/无 Cookie 的能力上限 | 中 | `app.py:62-65` | 部分房间或强风控场景无法监控（当前接受该限制） |

**成本侧结论**：本地约 4,926 行源码（`src/dlrecorder/**/*.py`），模块边界清晰，单元测试 209 项，属于**可维护规模**；真正的长期成本集中在 R1/R2 两点，而不是“代码太乱”或“重复上游太多”。

---

## 8. 与最初项目目标的符合程度

| 最初目标 | 现状 | 符合度 |
| --- | --- | --- |
| 以 `ihmily/DouyinLiveRecorder` 为核心技术基础 | 零代码继承；仅借鉴协议语义与通用做法 | **不符合（字面）** |
| 构建独立桌面可视化应用 | PySide6 四层 + core，完整 GUI、历史、设置、备份 | **符合** |
| 适配低配 Windows（i3/4GB） | 20 房间峰值约 60MB；低配真机验收仍未执行 | 部分符合（待 FU-001） |
| 数据安全优先 | SQLite 迁移校验、备份/恢复、磁盘保护、不删文件 | **符合（优于上游）** |
| 不擅自多平台扩张 | 仅抖音 | **符合** |
| 录制链路可持续值守 | 有自动录制，但无重连/流地址刷新 | **部分符合（R2）** |

需要区分两种“符合”：

- 如果把最初路线理解为**“复用上游代码/依赖上游包”**，那么项目**已经偏离**，且这种偏离是 2026-11-14 技术审查后的**主动选择**。
- 如果把最初路线理解为**“在成熟开源方案的工程经验之上做桌面产品”**，那么项目**基本符合**，但在上游最擅长、且最影响值守稳定性的两点（抗改版、断线续录）上**没有继承到位**。

---

## 9. 后续改进建议及优先级

> 以下仅为建议，不在本次审查中执行；任何改动需另行批准，且不得影响已发布的 0.1.1 与 `v0.1.1` Tag。

### P0 — 文档与定位（无代码风险）

1. 在 `README.md` 与 `THIRD_PARTY_NOTICES.txt` 增加“技术来源声明”：
   - 本项目为**独立实现**，未包含 `ihmily/DouyinLiveRecorder` 源代码；
   - 在直播状态语义（`status=2/4`）、TS 直封装、`q` 收尾等通用做法上参考了该项目的公开行为；
   - 记录审查基准 commit `add187f8`（2025-11-03）与日期。
2. 在 `docs/PROJECT_STATUS.md` 增加“架构定位”小节，明确 `douyin/` 是**可替换平台适配层**，避免后续把多平台代码堆进四层。

### P1 — 录制链路稳定性（下一版本核心）

3. 为 FFmpeg 命令补充重连与超时参数（参考上游 `main.py:1175-1204`，但按本地最小集裁剪）：`-rw_timeout`、`-reconnect_streamed`、`-reconnect_at_eof`、`-reconnect_delay_max`。
4. 设计“录制监督”能力：`RecordingService` 增加 `update_stream_url(room_id, url)` 或在同一 `recording_id` 下重启 FFmpeg（追加/续写同一 TS），并在监控循环成功解析后刷新地址。
5. 为自动录制增加**有限次退避重试**（如 5s/15s/30s，最多 3 次），避免失败即停或高频起停；手动录制保持现状并明确提示。
6. 增加流地址可用性探测（HEAD/短超时 GET），失败自动降档，与上游 `stream.py:65-69` 对齐。

### P1 — 解析健壮性

7. 增加第二数据源回退：公开页 → `web/enter` JSON（是否启用签名需单独评估与授权）；保持 `API_MALFORMED` 语义不变，绝不把异常当“下播”。
8. 为公开页解析建立**固定样本回归**（脱敏 HTML 片段），页面改版时可快速定位。
9. 清理或明确标注遗留路径（`extract_stream_url` / `classify_response`），避免误导。

### P2 — 上游情报同步（不合并代码）

10. 建立 `docs/UPSTREAM_WATCH.md`：每次上游 Release 记录版本、日期、与抖音相关的修复摘要、对本地的影响判断。
11. 在 CI 或人工流程中加入“上游 Release 检查”提醒（只读，不引入依赖）。

### P3 — 按需能力（不一次性引入）

12. 分段录制 / 转 MP4（用户诉求明确后再做，TS 单文件仍是当前安全默认）。
13. 状态推送（先做本机通知，再评估外部渠道，避免引入网络回调与凭据）。
14. 清晰度选择（需与 UI 和设置一起设计）。

### 不建议

- **不要直接 vendor 上游 `spider.py`**：会引入 `httpx/PyExecJS/Node`、约 160KB JS 与签名维护负担，且与本地“无凭据、轻量打包”的定位冲突。
- **不要为追求复用率引入上游包**：`project_memory` 已记录该风险（“直接依赖第三方录制解析库会导致对上游版本变更失去控制权”）。

---

## 10. 审查方法与可复核证据

| 项 | 内容 |
| --- | --- |
| 上游获取 | `https://codeload.github.com/ihmily/DouyinLiveRecorder/zip/refs/heads/main` → `%TEMP%\dlr-upstream`（独立临时目录，未进入工作区） |
| 上游版本 | `add187f8`（2025-11-03）；最新 Release `v4.0.7`（2025-10-24）；MIT |
| 本地版本 | `0.1.1`，Tag `v0.1.1` = `abcc7e0`；`git log` 共 4 个提交（2026-11-18） |
| 代码继承检查 | 全仓检索 `ihmily`、`X-Bogus`、`get_douyin`、`QUALITY_MAPPING`、`record_stream`：仅命中本地自研实现，无上游标识 |
| 依赖检查 | `pyproject.toml` 仅 `PySide6-Essentials`；无 `requests/httpx/PyExecJS/pycryptodome/loguru` |
| 远程检查 | `git remote -v` 输出为空 |
| 回归验证 | `.venv\Scripts\python.exe -m pytest -q` → **209 passed / 0 failed**（30.90s，离线） |
| 未验证项 | 未对上游程序做运行验证（仅静态阅读）；未测试上游签名绕过；未修改任何发布产物 |

### 10.1 本次审查未触碰的边界

- 未修改 `src/`、`tests/`、`release/`、`dist/`、`data/`；
- 未改动 `v0.1.0` / `v0.1.1` Tag 与历史提交；
- 未安装任何新依赖；
- 上游源码仅存在于 `%TEMP%\dlr-upstream`，不进入版本库。

---

**审查结论一句话**：当前项目是一套**独立实现**的抖音桌面录制工具，没有上游代码继承，也不具备上游同步机制；“脱节”是 2026-11-14 技术审查后的主动选择，而不是开发失控；但上游在**抗改版（多源回退/签名）**与**断线续录（reconnect/流地址刷新）**两方面的能力，恰是当前架构最薄弱处，应作为下一阶段（0.2.0）的首要改进方向。
