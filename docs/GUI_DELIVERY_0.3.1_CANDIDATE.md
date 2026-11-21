# DouyinLiveRecorder-Desktop GUI 候选版交付说明（0.3.1）

- 文档日期：2026-11-21
- 候选版本：0.3.1 GUI Candidate（Windows x64）
- 源码提交：`ee3a445`（GUI 源码）；交付文档与截图随后单独提交
- 状态：**候选交付，待用户验收；未覆盖正式版本，未打 v0.4.0 标签，未宣布正式发布**

---

## 1. 交付范围

本次交付在保持 Python + PySide6 + Qt Widgets 技术栈不变的前提下，完成 GUI 现代化升级：

- 全新信息架构：侧边栏 + 直播间 / 录制历史 / 运行日志 / 设置四页。
- 深浅双主题：跟随系统（默认）、浅色、深色，支持手动切换、持久化与系统主题变化自动跟随。
- 系统托盘：最小化隐藏主窗口与任务栏按钮、双击恢复、后台监控与录制继续、托盘菜单、退出安全确认与回退。
- 直播间状态胶囊、录制历史、日志着色与自动换行、统一中文对话框、空状态、深色原生标题栏。
- 高 DPI（125%）与最小窗口 900x600 适配。

未修改：解析引擎、监控/录制核心状态机、FFmpeg 核心逻辑、正式数据库 Schema、已封存 v0.3.1 发布物、正式用户数据。

---

## 2. 视觉设计说明

### 2.1 设计方向

使用 `frontend-design` 与 `web-app-development`（uicraft）插件辅助确定信息层级、设计令牌、排版密度、对比度与高 DPI 检查；所有 Web 风格建议均转译为 Qt Widgets 布局、QSS、QPalette 与自绘控件，未引入 HTML/CSS/JS/React/Vue/Tailwind 或任何新运行时依赖。

视觉定位：现代 Windows 桌面应用。关键词为现代、精致、清爽、克制、专业、直观、统一、轻量；避免通用后台模板、玻璃拟态、过度渐变与复杂动画。

### 2.2 设计令牌（同一体系，深浅两套）

| 语义 | 浅色 | 深色 |
| --- | --- | --- |
| 页面底色 bg | `#F5F6F8` | `#15171A` |
| 表面 surface | `#FFFFFF` | `#1E2126` |
| 浮层 elevated | `#FFFFFF` | `#2A2E34` |
| 边框 border | `#E3E6EA` | `#30353C` |
| 主文字 text | `#1B1F24` | `#E8EAED` |
| 次文字 text_secondary | `#5B6573` | `#A8B1BC` |
| 弱文字 text_muted | `#8A94A3` | `#7D8792` |
| 品牌强调 accent | `#E11D48` | `#E11D48` |
| 强调前景 accent_fg | `#C9183E` | `#FF708A` |
| 成功 success | `#17803D` | `#3FB950` |
| 警告 warning | `#B45309` | `#D29922` |
| 危险 danger | `#C62828` | `#F85149` |
| 信息 info | `#1D4ED8` | `#58A6FF` |

深色主题不是浅色反色：表面、边框、强调前景与状态色均按深色背景重新定值，保证对比度与状态辨识度。所有控件仅引用语义令牌，颜色集中在 `theme.py`。

### 2.3 字体、间距与圆角

- 界面字体：Microsoft YaHei UI 9pt；日志：Cascadia Mono。
- 圆角：控件 6px、卡片 10px、导航项 8px。
- 间距：页面外边距 24px，卡片内边距 16–20px，控件间距 8/12px。
- 主窗口默认 1120x740，最小 900x600；侧边栏固定 188px。
- 高 DPI：创建 QApplication 前设置 `PassThrough` 舍入策略，图标以 DPR=2 自绘。

### 2.4 页面要点

- **直播间管理**：7 列表格（ID 隐藏），名称列自适应；“监控/录制”用胶囊+圆点表达状态，原始单元格文本仍为“是/否”以兼容既有逻辑与测试；工具栏提供添加、开始/停止全部监控、刷新历史等主次操作。
- **录制历史**：文件、大小、时间、状态、路径清晰分列；时间由 UTC 转本地时区显示；空状态提供“刷新”入口。
- **运行日志**：最大 500 行，按级别着色，长文本按窗口宽度换行，不出现横向滚动条。
- **设置中心**：常规、录制、存储、外观、账户分区；外观页提供跟随系统/浅色/深色分段选择；数值输入保留可用编辑区。
- **添加直播间**：单对话框完成输入与校验，直播链接/房间号同一输入，错误就地提示。
- **对话框**：统一中文按钮、非原生文件选择器、深色原生标题栏、危险操作红色区分。

### 2.5 系统托盘行为

- 最小化：隐藏主窗口与任务栏按钮，保留托盘图标；监控与录制继续。
- 恢复：双击托盘图标或托盘菜单“显示主窗口”。
- 托盘菜单：显示主窗口、开始/停止全部监控、自动录制开关同步、退出。
- 关闭按钮：默认真正退出；录制中弹出安全确认（“停止录制并退出”为危险按钮，“继续录制”为默认按钮）；确认后停止录制并完成文件收尾再退出。
- 托盘不可用：`isSystemTrayAvailable()` 检测失败时安全回退，最小化保持普通窗口行为，不隐藏主窗口。

---

## 3. 主要修改文件

| 文件 | 说明 | 行数 |
| --- | --- | --- |
| `src/dlrecorder/presentation/theme.py` | 新增。语义令牌、浅/深主题、QSS/QPalette、系统跟随、持久化、DWM 深色标题栏 | 656 |
| `src/dlrecorder/presentation/icons.py` | 新增。自绘矢量图标、Logo、窗口图标（高 DPI） | 219 |
| `src/dlrecorder/presentation/pages.py` | 新增。侧边栏、直播间/历史/日志/设置页面 | 549 |
| `src/dlrecorder/presentation/tray.py` | 新增。托盘控制、菜单、恢复与退出信号 | 79 |
| `src/dlrecorder/presentation/main_window.py` | 重构主窗口装配、页面切换、托盘与安全退出 | 709 |
| `src/dlrecorder/presentation/widgets.py` | 组件、状态胶囊、空状态、对话框、日志与设置面板 | 1150 |
| `src/dlrecorder/application/app.py` | HiDPI、Qt 中文翻译、ThemeManager 注入 | 少量修改 |

提交统计：7 files changed，3142 insertions(+)，308 deletions(-)，提交 `ee3a445`。

---

## 4. 验证证据

### 4.1 自动化测试（真实 pytest，非界面模拟）

```text
.venv\Scripts\python.exe -m pytest -q
276 passed in 90.03s
```

覆盖既有业务逻辑、房间校验、录制与监控服务契约、设置持久化及原有 GUI 测试契约（表格列数、控件属性、日志上限等）。

### 4.2 GUI 自动化/模拟验收

验收脚本在隔离数据目录运行，使用本地 HTTP MPEG-TS 模拟流 + MockParser，但走真实 `RecordingService` 与真实 FFmpeg 进程；**属于自动化/模拟测试，不等同于真实抖音直播测试**。

原始证据：`docs/screenshots/gui-candidate-20261121/evidence.json`，关键结果：

- 平台 Windows，DPR 1.0，托盘可用 `true`。
- 最小化后：窗口不可见、无最小化任务栏状态、托盘图标仍可见；录制仍活跃；录制文件由 5,767,168 字节增长到 6,029,312 字节（`file_grew: true`）。
- 托盘恢复：窗口重新可见且未最小化；托盘“自动录制”菜单状态同步。
- 系统主题：Windows 当前为浅色时，默认“跟随系统”生效为浅色，标签显示“跟随系统 · 浅色”。
- 主题持久化：选择深色后设置值为 `dark`。
- 录制中关闭取消：点击“继续录制”后窗口仍可见、录制仍活跃。
- 录制中关闭确认：点击“停止录制并退出”后 `closing_flag: true`，录制列表清空，已完成文件 8,112,012 字节；历史中演示数据状态为 `interrupted`。
- 退出后无 FFmpeg 残留进程。

### 4.3 打包版冒烟与资源占用

- `build\build.ps1` 构建成功，产物 `dist\DouyinLiveRecorder`。
- 启动正常，窗口标题“抖音直播录制工具 v0.3.1”；发送关闭后优雅退出，退出码 0。
- 内置 FFmpeg：`_internal\ffmpeg\bin\ffmpeg.exe`。
- 空闲资源（打包版）：工作集 113.0 MB、私有内存 59.6 MB、线程 31、句柄 651；退出后无 FFmpeg 残留。

### 4.4 高 DPI 与窗口缩放

- `QT_SCALE_FACTOR=1.25` 下 dpr=1.25，主要页面无遮挡、无文字截断。
- 900x600 下直播间表格名称列仍可读，工具栏不溢出，设置页可滚动。

### 4.5 测试类型边界（重要）

| 类型 | 本次是否执行 | 说明 |
| --- | --- | --- |
| 自动化单元/集成测试 | 是 | 276 passed |
| 模拟流 GUI/录制验收 | 是 | 本地 HTTP MPEG-TS + MockParser + 真实 FFmpeg |
| 真实抖音直播录制 | 否（本轮未重新执行） | 解析/监控/录制核心未改动；真实能力沿用既有 0.3.1 验证，建议验收时补一次真实直播复核 |

---

## 5. 真实运行截图

目录：`docs/screenshots/gui-candidate-20261121/`

- 浅色：`light-rooms.png`、`light-history.png`、`light-logs.png`、`light-settings-top.png`、`light-settings-bottom.png`
- 深色：`dark-rooms.png`、`dark-history.png`、`dark-logs.png`、`dark-settings-top.png`、`dark-settings-bottom.png`
- 原生标题栏（含窗口边框）：同名 `*-frame.png`
- 空状态：`light-rooms-empty.png`、`dark-rooms-empty.png`、`light-history-empty.png`、`dark-history-empty.png`
- 添加直播间：`light-add-room-dialog.png`、`dark-add-room-dialog.png` 及 `-frame` 版本
- 最小窗口 900x600：`light-rooms-900x600.png`、`dark-rooms-900x600.png` 及 `-frame` 版本
- 托盘恢复：`light-tray-restored.png`
- 录制中退出确认：`light-exit-confirm.png`、`dark-exit-confirm.png`
- 125% 缩放：`dpi125/`（浅/深主要页面与设置页）

截图均为实际运行画面（`QWidget.grab()` 为客户区，`-frame` 为含系统标题栏的屏幕捕获）。

---

## 6. 候选发布包

| 项目 | 值 |
| --- | --- |
| 目录 | `release/DouyinLiveRecorder-0.3.1-gui-candidate-win64/` |
| 压缩包 | `release/DouyinLiveRecorder-0.3.1-gui-candidate-win64.zip` |
| 压缩包大小 | 81,528,302 字节 |
| SHA-256 | `B69F0532DA898F10FA696FF5888D60930A4122CB37872B1B46EBD3E929421FB9` |
| 源码提交 | `ee3a445` |
| 内置说明 | 候选目录内 `CANDIDATE_README.txt` |

候选包为独立目录，不覆盖 `release/DouyinLiveRecorder-0.3.1-win64/` 及对应 zip，未打 v0.4.0 标签。

---

## 7. 已知问题

- 尚未在低配置机器与 4K 多屏组合下逐一验证。
- 托盘依赖 Windows 通知区域；托盘不可用时回退为普通窗口，不隐藏主窗口。
- 极窄高度窗口下设置页需滚动查看全部选项。
- 本轮未重新执行真实抖音直播录制；真实直播能力建议在验收阶段补测一次。
- 功率/长时间运行稳定性（数小时连续录制、系统休眠唤醒）未在本轮覆盖。

---

## 8. 回滚说明

1. 候选版为独立目录，回滚无需卸载；直接继续使用原 0.3.1 正式版本。
2. 若候选版曾写入 `data/`，数据库 Schema 未变更，原 0.3.1 可继续读取；回滚前建议备份该目录下 `data/app.db` 与 `data/runtime/`。
3. 彻底清理：关闭程序后删除候选目录及压缩包。
4. 源码回滚：`git revert ee3a445` 或切回上一提交 `7d19dd3`。

---

## 9. 验收建议

1. 解压候选包，使用隔离数据目录或临时目录启动（可设 `DLR_DATA_DIR`）。
2. 分别切换浅色/深色/跟随系统，检查主要页面与对话框。
3. 添加一个真实直播间，验证监控启动、录制文件生成、历史显示与退出收尾。
4. 最小化到托盘，确认窗口与任务栏按钮隐藏、录制继续，双击恢复。
5. 录制中点击关闭，分别验证“继续录制”和“停止录制并退出”。

未经用户最终验收，本候选版不覆盖正式版本、不宣布正式发布。
