# GUI 候选版真实环境验收报告（0.3.1 Candidate）

- 报告日期：2026-11-21
- 验证性质：L2 受控验证（未开发、未改代码、未重新打包）
- 候选包：`release/DouyinLiveRecorder-0.3.1-gui-candidate-win64.zip`
- 结论：**真实验证受阻**——唯一有明确录制许可的房间 `95694179922` 当前未开播，按指令不使用其他房间替代。真实录制、托盘录制中行为、视频解码等项未执行。

---

## 1. 候选包完整性

| 项目 | 结果 |
| --- | --- |
| 交付报告记录 SHA-256 | `B69F0532DA898F10FA696FF5888D60930A4122CB37872B1B46EBD3E929421FB9` |
| 本次实际 SHA-256 | `B69F0532DA898F10FA696FF5888D60930A4122CB37872B1B46EBD3E929421FB9` |
| 压缩包大小 | 81,528,302 字节 |
| 压缩包时间 | 2026-11-21 16:51:41 |
| 可执行文件 SHA-256 | `137D96F713865135047A1E78E6A2DA0C7E3DA7109E7F199120B3A9943FB26596` |
| 包内 `data/`、`app.db` | 0 项（不含用户数据） |

结论：候选包与交付报告一致，未重新打包、未修改。

说明：解压目录中另有一个运行时生成的空 `data/`（0 房间、0 录制、0 设置），由启动冒烟产生，不属于 zip 内容；未改动 zip。

## 2. 版本与测试时间

- 程序版本：0.3.1（窗口标题“抖音直播录制工具 v0.3.1”，日志 `版本=0.3.1`）
- EXE 未写入 ProductVersion/FileVersion 资源，版本以应用日志与标题为准（已知问题）。
- 本次验证时间：2026-11-21 16:56–17:00（Asia/Shanghai）
- 运行方式：候选版 EXE + 隔离数据目录 `%TEMP%\dlr-accept-17c160f0`
- 验证期间无其他实例、无 FFmpeg 进程。

## 3. 真实解析器与 FFmpeg 启动记录

以候选版 EXE 启动（未设置 `DLR_PARSER`，即生产默认），隔离数据目录日志：

```text
2026-11-21 16:58:06 [INFO] dlrecorder.application.app: 应用初始化，版本=0.3.1，数据目录=...\dlr-accept-17c160f0\data
2026-11-21 16:58:06 [INFO] dlrecorder.application.app: FFmpeg 路径: ...\DouyinLiveRecorder-0.3.1-gui-candidate-win64\_internal\ffmpeg\bin\ffmpeg.exe
2026-11-21 16:58:06 [INFO] dlrecorder.application.app: 解析器: EngineDouyinParser，清晰度=UHD
```

结论：候选版默认使用生产 `EngineDouyinParser`，非 MockParser；内置 FFmpeg 路径正确。关闭后进程退出码 0，无 FFmpeg 残留。

## 4. 真实直播间可用性核查（阻断点）

对唯一有明确许可记录的房间 `web_rid=95694179922`（匿名会话，生产解析链路）连续请求两次：

| 时间 | 结果 |
| --- | --- |
| 2026-11-21 16:57 | `status=4`、`web_stream_url=null`、无 FLV/HLS 地址 |
| 2026-11-21 16:57 | 重试结果一致，仍为未开播 |

按用户指令：“如果没有已授权的在播房间，标注真实验证受阻，不擅自使用其他房间代替。”
因此未使用历史文档中出现过的其他公开房间（其本轮录制许可未确认），也未使用本地模拟流冒充真实录制。

## 5. 真实验收步骤执行结果

| 步骤 | 结果 |
| --- | --- |
| 1. GUI 添加真实直播间 | **阻断**（无可用的授权在播房间） |
| 2. 确认生产解析器、非 Mock | **已验证**（启动日志 `EngineDouyinParser`） |
| 3. 启动真实录制、FFmpeg 正常 | **阻断** |
| 4. 录制期间最小化到托盘 | **阻断**（本轮无真实录制；托盘机制见第 6 节与模拟证据） |
| 5. 观察 2–3 分钟文件持续增长与状态 | **阻断** |
| 6. 双击托盘恢复窗口 | **阻断**（同上） |
| 7. 录制中关闭并取消，录制继续 | **阻断** |
| 8. 再次退出并确认，收尾/DB/进程清理 | **阻断** |
| 9. 媒体信息与全文件解码 | **阻断** |

本轮**未产生真实录制文件**，不提供真实文件大小、增长曲线、媒体信息、解码结果或数据库录制记录。

### 5.1 模拟流证据（不计入真实验收）

既有 GUI 候选版模拟流验收（本地 HTTP MPEG-TS + MockParser + 真实 FFmpeg）结果保存在 [evidence.json](screenshots/gui-candidate-20261121/evidence.json)：最小化后文件 5,767,168 → 6,029,312 字节、托盘恢复、取消/确认退出均通过。该结果**仅证明 GUI 与录制服务接线**，不作为真实抖音录制证据。

## 6. 额外复核（基于变更文件的静态审查 + 只读检查）

### 6.1 设置与数据库结构兼容性

- `git diff 7d19dd3 ee3a445` 显示数据库、迁移、Schema、核心录制代码零改动；依赖文件零改动。
- 主题偏好仅使用既有 `settings(key, value)` 表写入 `appearance.theme`，无新表、无新列：
  - 读取/保存见 [theme.py](../src/dlrecorder/presentation/theme.py#L624-L639)
  - 通用键值接口见 [db.py](../src/dlrecorder/infrastructure/db.py#L744-L757)
- 只读对比候选版新建库与 0.3.1 基线库：

| 数据库 | Schema 版本 | 表数量 |
| --- | --- | --- |
| 候选版隔离库 | 2 | 6 |
| 0.3.1 packaged 基线 | 2 | 6 |
| 0.3.1 source 基线 | 2 | 6 |

结论：结构兼容，无迁移需求。

### 6.2 关闭与退出清理是否重复执行

调用链：`closeEvent` → 录制/监控服务 shutdown → `window.dispose()` → `app.quit()`；`__main__` 在 `run()` 返回后再次调用 `Application.shutdown()`。

重复调用均有幂等保护，未发现重复收尾或重复写库：

- 主窗口 `closeEvent` 以 `_closing` 提前返回：[main_window.py](../src/dlrecorder/presentation/main_window.py#L745-L780)
- `dispose()` 可重复调用：[main_window.py](../src/dlrecorder/presentation/main_window.py#L721-L726)
- 录制服务缓存关闭报告：[recording_service.py](../src/dlrecorder/application/recording_service.py#L315-L337)
- 应用级 `_shutdown_done` 守卫：[app.py](../src/dlrecorder/application/app.py#L274-L296)
- 数据库 `close()` 空值守卫：[db.py](../src/dlrecorder/infrastructure/db.py#L71-L75)
- 日志桥接 `close()` 只解绑一次：[widgets.py](../src/dlrecorder/presentation/widgets.py#L516-L520)
- 托盘 `dispose()` 置空并删除：[tray.py](../src/dlrecorder/presentation/tray.py#L85-L93)

### 6.3 最小化/恢复是否可能导致重复录制

- 最小化路径仅在 `isMinimized()` 且非退出中时 `hide()`，不调用监控或录制启动接口：[main_window.py](../src/dlrecorder/presentation/main_window.py#L239-L249)
- 恢复路径仅 `showNormal/activateWindow/raise_`：[main_window.py](../src/dlrecorder/presentation/main_window.py#L251-L255)
- `changeEvent` 只投递隐藏动作，不触发录制：[main_window.py](../src/dlrecorder/presentation/main_window.py#L735-L743)
- 录制本身由 `RecordingService` 按房间 ID 管理，UI 显示/隐藏不参与其状态机。

结论：代码层面不存在最小化/恢复导致重复录制的路径；真实录制下仍建议按验收步骤实测确认。

### 6.4 托盘不可用回退

- `QSystemTrayIcon.isSystemTrayAvailable()` 为假时不创建托盘，记录告警并保持普通窗口行为：[tray.py](../src/dlrecorder/presentation/tray.py#L27-L33)
- `_setup_tray` 在无托盘时置 `_tray=None`、恢复 `quitOnLastWindowClosed(True)`、侧边栏提示“系统托盘不可用”，关闭按钮走正常退出：[main_window.py](../src/dlrecorder/presentation/main_window.py#L218-L237)
- 无托盘时 `changeEvent` 不满足 `_tray is not None`，不会隐藏窗口。

结论：静态路径完整；本轮未在真实“无通知区域”环境做运行时模拟（剩余风险见第 8 节）。

### 6.5 GUI 改动边界

`git diff --stat 7d19dd3 ee3a445`：

```text
src/dlrecorder/application/app.py          |   15 +-
src/dlrecorder/presentation/icons.py       |  238 +
src/dlrecorder/presentation/main_window.py |  678 +-
src/dlrecorder/presentation/pages.py       |  630 +
src/dlrecorder/presentation/theme.py       |  699 +
src/dlrecorder/presentation/tray.py        |   93 +
src/dlrecorder/presentation/widgets.py     | 1097 +-
```

`core`、`domain`、`infrastructure`、解析器、FFmpeg、数据库 Schema、依赖清单均无改动。

## 7. 已验证 / 未验证 / 阻断

| 类别 | 事项 | 状态 |
| --- | --- | --- |
| 已验证 | 候选包 SHA-256 与交付报告一致 | 通过 |
| 已验证 | 候选版默认 `EngineDouyinParser`、非 Mock | 通过 |
| 已验证 | 内置 FFmpeg 路径正确、正常启动/退出（退出码 0、无残留） | 通过 |
| 已验证 | 数据库 Schema 与 0.3.1 基线一致（version 2 / 6 表） | 通过 |
| 已验证 | 退出清理幂等、最小化/恢复不触发录制、托盘回退静态路径 | 通过（静态） |
| 未验证 | 真实直播添加、录制、文件增长、托盘录制中行为、退出收尾、解码 | 未执行 |
| 阻断 | 授权房间 `95694179922` 两次核查均为未开播（status=4） | 真实验证受阻 |

## 8. 剩余风险

1. 真实直播链路（解析→录制→收尾→解码）在本候选版上仍未取得端到端证据。
2. 托盘在本轮未做真实桌面双击/无托盘环境的运行时复测，仅有代码审查与上一轮模拟流证据。
3. 未为托盘/主题/最小化行为新增自动化测试；本轮遵守“不开发”边界。
4. EXE 无版本资源，部署侧只能通过标题或日志确认版本。
5. `TrayController` 构造未包裹异常；若 Qt 创建托盘对象本身抛错（非“不可用”返回假），缺少兜底。概率低，但属残余风险。
6. 未验证数小时长录、休眠唤醒与低配机表现。

## 9. 解除阻断条件

提供以下任一条件后即可继续第 5 节全部步骤：

1. 一个**有录制许可且正在开播**的直播间 ID（或直播链接）；或
2. 授权房间 `95694179922` 开播后通知复测。

复测将使用独立数据目录、候选版原包，不改代码、不重新打包；保留文件增长、托盘恢复、退出收尾、解码与数据库记录，并对日志中的 Cookie/Token/完整流地址做脱敏。

## 10. 本轮操作边界声明

- 未修改正式 v0.3.1 发布物与用户数据。
- 未修改或重新打包候选版（zip SHA-256 未变）。
- 未创建标签、未宣布发布。
- 未使用模拟流冒充真实录制。
