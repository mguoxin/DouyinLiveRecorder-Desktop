# PROJECT-AUDIT — DouyinLiveRecorder-Desktop 工程审计

> 审计基线：仓库 `mguoxin/DouyinLiveRecorder-Desktop`，提交 `c8ec7c2`，标签 `v0.4.0`（Release 发布于 2026-11-21T15:26:36Z）
> 审计日期：2026-11-21
> 审计模式：L2 只读静态审计（未修改任何源码、配置、数据库或发布产物）
> 测试基线：276 passed（Windows / Python 3.12.16 / pytest 9.2.0，发布前重跑 134.39 s）。本轮未重跑，仅引用既有记录。

---

## 1. 审计方法与本轮局限

- 静态阅读源码、文档、构建脚本与测试；用 `grep`/`Select-String` 做分层依赖与符号定位。
- **未执行**：真实直播录制、GUI 手动操作、长时稳定性、性能剖析、数据库大数据量压测。
- 因此本报告把结论分为三类：**已验证缺陷**（有代码+日志/数据库等客观证据）、**潜在风险**（代码路径成立但未复现）、**建议**（主观优化）。
- 凡涉及性能、内存、并发吞吐的判断一律标注「待测」，不作为瓶颈结论。

---

## 2. 项目结构与模块职责

```
src/dlrecorder/
├─ __main__.py              启动入口：单实例互斥 → Application → 原生错误框
├─ application/             用例编排层
│  ├─ app.py                组合根（唯一允许同时依赖 presentation 与 infrastructure）
│  ├─ monitor_service.py    监控轮询、任务并发、关闭顺序
│  ├─ recording_service.py  录制状态机、FFmpeg 生命周期、续录、收尾（882 行）
│  └─ settings_service.py   设置读写与校验
├─ core/task_manager.py     监控任务线程池与取消令牌
├─ domain/                  纯领域模型
│  ├─ models.py             房间/链接校验
│  ├─ states.py             监控与录制状态枚举
│  └─ recording.py          录制分段数据结构
├─ douyin/                  解析层
│  ├─ parser.py             解析器选择与错误映射
│  └─ engine/               上游移植引擎（douyin.py、ab_sign.py、parser.py）
├─ infrastructure/          技术实现
│  ├─ db.py                 SQLite Schema v2、V1→V2 迁移、CRUD（794 行）
│  ├─ ffmpeg.py             FFmpeg Popen、三级停止（q→terminate→kill）
│  ├─ winproc.py            Windows Job Object
│  ├─ process_registry.py   进程登记表（PID + 创建时间 + 路径）
│  ├─ orphan.py             启动时残留进程核验与清理
│  ├─ backup.py             备份/恢复（Backup API + 完整性校验）
│  ├─ credential_store.py   Cookie 的 DPAPI 加密存储
│  ├─ logging_config.py     日志轮转与第三方库降噪
│  ├─ disk.py               磁盘剩余空间
│  ├─ http_client.py / http_executor.py   HTTP 错误分类与重试
│  ├─ config.py             路径与常量
│  └─ single_instance.py    Local\ 命名互斥量
└─ presentation/            表现层
   ├─ main_window.py        主窗口、托盘装配、关闭流程（718 行）
   ├─ pages.py              房间/历史/日志/设置页面
   ├─ widgets.py            通用控件与对话框（1150 行）
   ├─ tray.py               托盘控制器
   ├─ theme.py              深浅主题与中文翻译
   └─ icons.py              品牌图标绘制
```

---

## 3. 分层与依赖评估

| 检查项 | 结果 | 依据 |
| --- | --- | --- |
| `domain` 是否反向依赖上层 | 通过（0 处） | `domain/*.py` 无 `..application/.presentation/.infrastructure` 导入 |
| `infrastructure` 是否依赖应用/表现层 | 通过（0 处） | 同上 |
| `core` 是否依赖表现层 | 通过（0 处） | 同上 |
| 应用层是否依赖表现层 | 仅 `app.py:29-30` 导入 `MainWindow`/`ThemeManager` | 组合根，合理 |
| 表现层依赖 | 通过 `application` 服务与 `infrastructure.db/ffmpeg/backup` | 桌面单进程应用的常规做法 |

结论：**分层方向正确**，没有循环依赖。`app.py` 承担组合根，`presentation` 不直接操作 SQL，录制子进程只在 `application.recording_service` 的管理线程中启停，GUI 通过 Qt 信号接收结果（`recording_service.py:3-12` 的模块说明与 `start_recording`/`stop_recording` 的队列投递实现一致）。

需要说明的耦合点（**建议级**，非缺陷）：

1. `presentation.main_window` 直接构造 `MonitorService`/`RecordingService`/`Database`（`main_window.py` 顶部导入），缺少一层「应用门面」。当前规模可接受，拆分收益有限。
2. `presentation.theme` 直接读 `Database`，主题持久化绕过了 `SettingsService`。改动收益小，但会让设置读写路径出现第二条事实来源。
3. `widgets.py`（1150 行）与 `pages.py` 混合了控件、对话框和业务校验调用，是后续 GUI 迭代时最容易被继续堆大的文件。

---

## 4. 关键代码路径

### 4.1 录制启动

`MainWindow._start_recording` → `RecordingService.start_recording`（队列投递）→ 管理线程内：重复录制检查 → 磁盘空间检查（`disk.py`）→ 解析流地址 → `Database.start_recording` 写入 `status='recording'` → `FFmpegProcess.start` → `recording_started` 信号回主线程。

### 4.2 录制结束与收尾

`FFmpegProcess.poll` 返回退出码 → `_handle_exit`（`recording_service.py:580-632`）→ `_clean_exit_outcome`（`:611`）重新解析直播状态 → 走 `_close_segment`（正常收尾，`:857-915`）或 `_begin_reconnect`（`:634`）→ 重试耗尽/手动停止时走 `_end_without_segment`（`:819-841`）。

### 4.3 进程残留防护

`Popen` 创建后登记 `(pid, 创建时间, exe 路径)`（`process_registry.py:62-105`）并加入 Windows Job Object（`winproc.py:180-233`）→ 应用异常结束时由系统终止子进程 → 下次启动 `orphan.py` 仅在三项身份全部匹配时终止遗留进程。

---

## 5. 整体质量评估

**做得好的部分（不建议改动）**

| 能力 | 证据 |
| --- | --- |
| 分层与线程边界 | 录制启停全部走队列，GUI 不碰子进程；数据库写入集中在管理线程 |
| 数据迁移安全 | V1→V2 在写锁+显式事务内执行，迁移前做一致性备份，失败回滚并抛出（`db.py:228-410`），迁移后做快照/外键/对象/自增序列校验（`db.py:513-607`） |
| 进程残留防护 | 三重身份核验 + Job Object，避免 PID 复用误杀（`orphan.py`、`winproc.py`） |
| 凭据保护 | Cookie 走 DPAPI（`credential_store.py:70-89`），日志只记长度 |
| 日志降噪 | `httpx/httpcore/h2` 完整 URL 日志降到 WARNING（`logging_config.py:31-34`） |
| 交付可重复性 | `build/build.ps1` + `pyinstaller.spec` + `verify_icon.py` + 发布包 SHA-256 与 `SHA256SUMS.txt` |

**主要短板**

1. 事件契约不统一（P2-01，见 `P2-01-ANALYSIS.md`）。
2. 仓库对外呈现滞后于实际状态：README 仍写 0.1.1 发布包路径（`README.md:161`），全文无 GitHub Releases 链接。
3. 无 CI、无 LICENSE，工程流程只有本地脚本。
4. FFmpeg stderr 被丢弃（`ffmpeg.py:228-245`），导致「未知退出码」类问题缺少诊断证据。
5. `recordings` 表无二级索引（见 `DEFECT-REGISTER.md` PR-05，影响待测）。

**规模事实（不作为缺陷依据）**

| 文件 | 行数 |
| --- | --- |
| `presentation/widgets.py` | 1150 |
| `application/recording_service.py` | 882 |
| `infrastructure/db.py` | 794 |
| `presentation/main_window.py` | 718 |

大于 700 行的文件集中在录制服务、数据库与 GUI 控件。当前无证据表明这些文件造成缺陷，属于维护成本观察项：**不建议为「拆文件」而重构**，只在有明确改动需求时顺带拆分。

---

## 6. 结论

项目整体处于「功能闭环、数据安全设计扎实、工程质量高于同类个人项目」的水平。风险集中在**收尾事件的语义一致性**、**对外发布入口的文档滞后**、**缺少自动化质量门禁**和**FFmpeg 诊断证据缺失**四件事上；这些都是小批量、可回退的改动，不需要架构级重构。
