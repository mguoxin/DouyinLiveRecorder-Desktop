# DEFECT-REGISTER — 缺陷与风险登记（v0.4.0 基线）

> 基线提交 `c8ec7c2` / 标签 `v0.4.0`。审计方式为只读静态审计，未修改任何文件。
> 类别定义：
> - **已验证缺陷**：有代码事实 + 日志/数据库/仓库状态等客观证据，可复现或可复核。
> - **潜在风险**：代码路径成立、条件可构造，但本轮未复现。
> - **建议**：主观优化，不修也不会出错。
> - **已排除**：曾有怀疑，审计后证明不成立。
>
> 严重度：S1 高（影响数据/安全/不可用）｜S2 中（影响可用性或对外发布质量）｜S3 低（体验/维护）

---

## 汇总

| ID | 类别 | 标题 | 严重度 | 证据位置 |
| --- | --- | --- | --- | --- |
| V-01 | 已验证缺陷 | 会话结束事件与分段结束事件字段混用（P2-01） | S2 | `recording_service.py:819-841`、`:905-915`、`main_window.py:505-520` |
| V-02 | 已验证缺陷 | README 发布入口停留在 0.1.1，无 Release 链接 | S2 | `README.md:161`（全文无 `github.com` 链接） |
| V-03 | 已验证缺陷 | 仓库无 LICENSE、无 CONTRIBUTING/Issue 模板 | S2 | `git ls-files` 根目录仅 5 个文件，无许可证文件 |
| V-04 | 已验证缺陷 | 仓库无 CI，无任何 `.github/` 配置 | S2 | `git ls-files` 匹配 `.github/` 数为 0 |
| PR-01 | 潜在风险 | 关闭时同步 join 最长 30 s，界面可能无响应 | S3 | `main_window.py:754-789`、`recording_service.py:315-321`、`:50` |
| PR-02 | 潜在风险 | 录制失败提示仅状态栏，用户易错过 | S3 | `main_window.py:185-199`、`recording_service.py:959-961` |
| PR-03 | 潜在风险 | 日志写入本机绝对路径 | S3 | `recording_service.py:870-885`、`main_window.py:516-522` |
| PR-04 | 潜在风险 | FFmpeg stderr 被丢弃，未知退出码无法诊断 | S2 | `ffmpeg.py:228-245` |
| PR-05 | 潜在风险 | `recordings` 无二级索引，历史查询全表扫描（影响待测） | S3 | `db.py:140-227`、`:827-849` |
| PR-06 | 潜在风险 | 失败原因不可查询（无 `error_message` 列） | S3 | `db.py:789-808` 注释明示的设计取舍 |
| PR-07 | 潜在风险 | 单实例互斥量为 `Local\` 会话级 | S3 | `single_instance.py:17-18` |
| PR-08 | 潜在风险 | 退出码 0 且解析失败时状态归为 unknown | S3 | `recording_service.py:611-632` |
| S-01 | 建议 | 事件契约增加 `scope`（segment/session） | — | 见 `P2-01-ANALYSIS.md` |
| S-02 | 建议 | FFmpeg stderr 脱敏落盘 | — | 同 PR-04 |
| S-03 | 建议 | README 首屏重写（下载/截图/要求/已知问题） | — | 见 `GITHUB-IMPROVEMENTS.md` |
| S-04 | 建议 | GitHub Actions 分层 CI | — | 见 `GITHUB-IMPROVEMENTS.md` |
| X-01 | 已排除 | 托盘对象被 GC 回收 | — | `main_window.py:219`、`:230` 持有 `self._tray` |
| X-02 | 已排除 | 主线程直接阻塞等待 FFmpeg | — | 启停均在管理线程，GUI 只投递队列 |
| X-03 | 已排除 | 数据库线程不安全 | — | `db.py:47-64` `RLock` + 单连接串行化 |

---

## 已验证缺陷

### V-01 会话结束事件与分段结束事件字段混用（P2-01）

- **现象**：直播自然结束、续录重试耗尽或「等待续录期间手动停止」时，日志/状态栏可能显示 `completed size=0`。
- **代码事实**：
  - `_end_without_segment`（`recording_service.py:819-841`）在没有任何活动分段时仍发出 `recording_finished`，载荷为 `recording_id=None`、`file_path=str(active.file_path)`（指向**从未创建**的下一分段路径）、`file_size=0`。
  - 正常分段收尾（`:905-915`）使用同一事件名与同一组字段，但 `recording_id` 与 `file_size` 是真实值。
  - GUI 消费端 `main_window._on_recording_finished`（`main_window.py:505-520`）以 `snapshot.get("file_size", 0)` 直接展示，无法区分两种语义。
- **影响**：仅运行期展示与日志误导；数据库记录（`finish_recording` 在 `db.py:789-808` 幂等更新）与录制文件不受影响。这是 v0.4.0 Release 已登记的已知问题。
- **复核方式**：阅读上述三处即可确认；真实日志证据见 `docs/AUDIT_0.3.1_RECORDING_STATUS.md` 第 11 节与 `docs/RELEASE_0.4.0.md` 已知问题 1。
- **处置**：见 `P2-01-ANALYSIS.md`（最小修复方案，不改 Schema）。

### V-02 README 发布入口停留在 0.1.1

- **代码事实**：`README.md:161` 仍写「正式发布包：`release\DouyinLiveRecorder-0.1.1-win64.zip`（约 75MB）」；全文检索 `github.com` 无命中，即**没有任何指向 GitHub Releases 的下载链接**。
- **影响**：新用户从 GitHub 仓库页进入 README 后，看到的是本地相对路径（仓库中并不存在该 zip）与过期版本号，无法在 30 秒内找到 0.4.0 正式下载入口。这是发布渠道层面的实际缺陷。
- **复核方式**：`Select-String -Path README.md -Pattern 'releases|0\.1\.1|github\.com'`。
- **处置**：见 `GITHUB-IMPROVEMENTS.md` G-01。

### V-03 无许可证与贡献规则

- **事实**：`git ls-files` 根目录仅 `.gitignore`、`README.md`、`THIRD_PARTY_NOTICES.txt`、`pyproject.toml` 与 `阶段3A报告.md`；无 `LICENSE`、`COPYING`、`CONTRIBUTING.md`、`CODE_OF_CONDUCT.md`。
- **影响**：对外公开分发时，使用者无法确定授权范围；第三方组件（PySide6/LGPL、FFmpeg/GPL 或 LGPL 取决于构建）的分发义务仅有 `THIRD_PARTY_NOTICES.txt` 一份说明。**本节只陈述事实缺口，不构成法律结论**，最终选择需专业判断。
- **处置**：见 `GITHUB-IMPROVEMENTS.md` G-03。

### V-04 无 CI

- **事实**：仓库无 `.github/` 目录（`git ls-files` 匹配数 0）；276 项回归只在本地执行。
- **影响**：任何后续提交都没有自动质量门禁；发布前「重跑 276 项」依赖人工记忆。
- **处置**：见 `GITHUB-IMPROVEMENTS.md` G-05（已确认测试具备无头运行基础：`tests/conftest.py:7-24` 设置 `QT_QPA_PLATFORM=offscreen` 与 `DLR_PARSER=mock`）。

---

## 潜在风险

### PR-01 关闭流程同步等待，最长 30 s

- `main_window.closeEvent`（`:754-789`）在事件循环内调用 `self._recording.shutdown()` → `self._service.shutdown()` → `app.quit()`。
- `RecordingService.shutdown`（`:315-321`）对管理线程 `join(self._shutdown_timeout_s)`，默认 30 s（`:50`）。
- **风险**：若管理线程卡在 FFmpeg 停止或数据库写入，窗口在关闭过程中无响应，用户可能强杀进程。强杀后 FFmpeg 由 Job Object 兜底终止，数据一致性有 `finish_recording` 幂等保护，因此定级 S3。
- **验证方式（待做）**：注入一个 sleep 的伪 FFmpeg 停止路径，测量 close 到退出的耗时与窗口响应性。

### PR-02 录制失败的用户可见性弱

- `recording_service.py:959-961` 的错误仅 `logger.warning` + `recording_error.emit`；`main_window.py:185-199` 把错误写进状态栏/日志页，无对话框或托盘气泡。
- **风险**：用户最小化到托盘时不会注意到录制失败，直到回看历史才发现缺失。

### PR-03 日志记录本机绝对路径

- `recording_service.py:870-885` 与 `main_window.py:516-522` 在完成/失败日志里输出 `file_path`。
- **风险**：用户把 `app.log` 贴到 Issue 时会暴露 Windows 用户名与目录结构。`app.log` 仅本地落盘（`logging_config.py:10-34`，5 MB×3 轮转），无外发行为。

### PR-04 FFmpeg stderr 被丢弃

- `ffmpeg.py:228-245` 以 `stdout=DEVNULL, stderr=DEVNULL` 启动；退出码是唯一证据。
- **风险**：Release 说明中登记的「FFmpeg 偶发未知退出码（如 3753488571）」在没有 stderr 的情况下无法定性。这是**诊断能力缺失**，不是已确认的录制缺陷。
- **注意**：FFmpeg 命令行含带签名的流地址，采集时必须只落 stderr 内容、不落命令行。

### PR-05 `recordings` 无二级索引（影响待测）

- 建表（`db.py:140-227`）除主键与 `rooms.web_rid UNIQUE` 外无其他索引；`list_recordings`（`:827-849`）按 `room_id`/`status` 过滤 + `id DESC LIMIT 200`。
- **风险**：历史记录量级大时可能全表扫描。**未测量**，不得据此宣称瓶颈。现有 `LIMIT 200` 与桌面单用户场景下，1 万行量级预计无感；建议先测量再决定（见 `V0.4.1-ROADMAP.md` P2-2）。

### PR-06 失败原因不可查询

- `recordings` 表无 `error_message` 列（`db.py:797` 注释明确「失败原因只写日志，不改变 Schema」）。
- **风险**：无法在 GUI 历史页筛选/展示失败原因，只能翻日志。属于已确认的**设计取舍**，不是缺陷。

### PR-07 单实例互斥量为会话级

- `single_instance.py:17-18` 使用 `Local\DouyinLiveRecorder-Desktop.SingleInstance`，作用域为当前登录会话。
- **风险**：快速用户切换或多用户同时登录时，可各启动一个实例；若两者指向同一 `data\` 目录（例如放在共享盘），会出现双写。默认安装路径在用户桌面目录下，实际概率低。

### PR-08 退出码 0 且解析失败时状态归为 unknown

- `_clean_exit_outcome`（`:611-632`）在「进程正常退出」后重新解析直播状态；解析异常时归为 unknown，交由上层按重试预算决定收尾或续录。
- **风险**：网络抖动叠加直播自然结束时，可能多走一次续录再结束，表现为多一条空分段记录。未复现。

---

## 已排除的怀疑

### X-01 托盘对象被 GC

`main_window.py:219` 声明 `self._tray: Optional[TrayController] = None`，`:230` 赋值 `self._tray = tray`，生命周期由主窗口持有，不存在「局部变量被回收导致托盘消失」的问题。

### X-02 GUI 主线程等待 FFmpeg

`start_recording`/`stop_recording` 均通过队列投递（`recording_service.py:180-193`），FFmpeg 启停与轮询在管理线程执行，未发现主线程 `wait()`/`sleep()`。唯一的主线程同步等待是 PR-01 的关闭路径。

### X-03 数据库线程不安全

`Database` 用 `RLock` 串行化全部访问，连接为 `check_same_thread=False` 的单连接（`db.py:47-64`）。这是**有意设计**（写操作全部在管理线程，读操作也持锁），当前未发现可构造的竞态。

---

## 与旧台账的编号对应

`docs/AUDIT_0.3.1_RECORDING_STATUS.md` 第 11.2 节已登记 R-01/R-02/R-03/D-01 四项。本登记表为避免编号冲突改用 `PR-*`，对应关系如下：

| 旧台账 | 本登记表 | 说明 |
| --- | --- | --- |
| P2-01 | V-01 | 同一问题，本表补充了代码行级证据与消费端位置 |
| R-01（FFmpeg 三次未知退出，`exit=3753488571`） | PR-04 | 本表把「stderr 被丢弃」单独列为诊断缺口，未改变原结论 |
| R-02（解析侧 SSL `UNEXPECTED_EOF_WHILE_READING`） | PR-08 | 属外部网络因素，本表不推断根因 |
| R-03（风控验证页中断） | 未单列 | 外部触发条件不明，且与 v0.4.0 代码无关；保留在旧台账 |
| D-01（诊断信息缺失） | PR-04、PR-06、PR-03 | 拆成「FFmpeg 无 stderr」「无 error_message 列」「日志含绝对路径」三条 |
