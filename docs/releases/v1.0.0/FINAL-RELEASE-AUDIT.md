# DouyinLiveRecorder-Desktop v1.0.0 发布前最终专项审计报告

- 审计日期：2026-11-28（Asia/Shanghai）
- 唯一工作目录：`C:\Users\kglpl\Desktop\DouyinLiveRecorder-Desktop`
- 分支：`feat/v1.0.0`
- 审计起点 HEAD：`65bc3d5b1e4ac58932a73ef474c20a0b0d50af9d`
- 审计修复提交：`8ec6c55`（表格溢出）、`b348ab9`（最小窗口尺寸）
- v1.0.0 源码基线：`ad7f6c6`
- v0.5.0 冻结基线：`34c42fee312cf70bb77a24951e69fd08b6c15be4`
- 数据库 Schema：v4
- 远端：`origin` = `https://github.com/mguoxin/DouyinLiveRecorder-Desktop.git`
- 当前发布状态：**候选构建完成，未合并、未打标签、未公开发布**

> 状态标签严格区分：**PASS / FAIL / BLOCKED / NOT TESTED**。未实际执行的项目不使用 PASS。

---

## 1. 项目基线与 Git 状态

| 核查项 | 实测结果 | 状态 |
| --- | --- | --- |
| 唯一 Git 仓库 | Desktop 目录（`git worktree list` 仅 1 项） | PASS |
| 分支 | `feat/v1.0.0` | PASS |
| 审计前 HEAD | `65bc3d5`，与历史报告一致 | PASS |
| 审计修复提交 | `8ec6c55`（表格溢出）、`b348ab9`（最小窗口 1024×640） | PASS |
| v0.5.0 冻结提交 | `34c42fe`（`feat/v0.5.0`，bundle 内可恢复） | PASS |
| 已跟踪文件 | 干净；`git status --porcelain` 仅剩 6 个未跟踪目录（审计/构建临时物） | PASS |
| stash / 额外 worktree | 无 | PASS |
| 推送状态 | `feat/v1.0.0` 已推送至 origin（`65bc3d5..8ec6c55`），未强制推送、未覆盖他人提交 | PASS |

未跟踪目录：`_audit/r3_independent_review/`、`_audit/r4_delivery/`、`_audit/r4_remediation/`、`build/_work_v050/`、`build/_work_v100/`、`dist_v050/`（均不属于源码基线，未纳入提交）。

## 2. 目录结构核查

| 目录 | 内容 | 结论 |
| --- | --- | --- |
| `DouyinLiveRecorder-Desktop` | 唯一源码、`.git`、`.venv`、`dist/`、`build/` | PASS（唯一开发目录） |
| `DouyinLiveRecorder-Backups` | 仅静态快照：`v0.5.0_34c42fe_20261128_141557`、`v1.0.0_65bc3d5_20261128_152736`（另见 §3 补充快照） | PASS（无 `.git`、无源码开发） |
| 并行源码/第二套环境 | 不存在 | PASS |

审计期间未移动、合并或删除任何既有目录；未在备份目录内开发。

## 3. 备份完整性与恢复验证

### 3.1 现有 v0.5.0 快照（审计复核）

| 检查项 | 结果 | 状态 |
| --- | --- | --- |
| 文件数 | 12 个文件 + `RESTORE.md`，约 121 MB | PASS |
| `git bundle verify` | 完整历史，22 个引用（含 `feat/v0.5.0`=34c42fe、7 个标签、远端跟踪） | PASS |
| `SHA256SUMS.txt` | 12/12 全部一致 | PASS |
| ZIP 可读性 | `worktree_tracked` 718 项、`untracked_docs` 525、`untracked__audit` 64、`untracked_acceptance` 9、候选包 210 项 | PASS |
| 候选包 | 109,217,601 字节，`B51AB48E…C697` | PASS |
| `RESTORE.md` | 含校验、克隆/取回、候选包校验命令 | PASS |
| 隔离恢复演练 | `git clone <bundle>` 后 `checkout 34c42fe` 成功，652 个跟踪文件，HEAD 一致 | PASS |

### 3.2 新增 v1.0.0 快照

新建（未覆盖旧快照）：`C:\Users\kglpl\Desktop\DouyinLiveRecorder-Backups\v1.0.0_65bc3d5_20261128_152736`

| 文件 | 说明 |
| --- | --- |
| `repo_all_refs.bundle` | 23 个引用（含 `feat/v1.0.0`=65bc3d5、7 标签、远端跟踪），`verify` 通过、完整历史 |
| `worktree_tracked_65bc3d5.zip` | `git archive` 生成，910 条目（772 跟踪文件 + 目录） |
| `untracked_docs.zip` / `untracked__audit.zip` | 527 / 64 条目 |
| `DouyinLiveRecorder-v1.0.0-candidate.zip` | 冻结候选包副本 |
| `HEAD.txt`/`git_refs.txt`/`tags.txt`/`branches.txt`/`git_status.txt`/`worktree_list.txt`/`backup_inventory.txt` | 冻结状态 |
| `SHA256SUMS.txt` / `RESTORE.md` | 12 项哈希全部一致；恢复步骤可执行 |

| 检查项 | 结果 | 状态 |
| --- | --- | --- |
| bundle 恢复 | 克隆后 `checkout 65bc3d5` 成功，772 跟踪文件，HEAD 一致 | PASS |
| 快照完整性 | 12/12 SHA-256 一致 | PASS |
| 凭据泄露 | 备份仅含源码/文档/候选包/状态文本，无 Cookie/Token/密码 | PASS |
| 用户录制数据 | 未复制、未移动、未删除 | PASS |
| 覆盖范围 | 该快照早于 UI 修复提交 `8ec6c55` 与 r2 构建，已在 §15 补充最终快照 | 已说明 |

## 4. 候选包完整性与哈希

| 项目 | 原始候选（保留） | r2（中间构建，已取代） | **r3（审计最终）** |
| --- | --- | --- | --- |
| 文件 | `dist\DouyinLiveRecorder-v1.0.0-candidate.zip` | `…-candidate-r2.zip` | `dist\DouyinLiveRecorder-v1.0.0-candidate-r3.zip` |
| 大小 | 109,699,513 字节 | 109,700,554 字节 | 109,699,573 字节 |
| SHA-256 | `B59250227EBE51D7CBF283121F6555416FB3BB78913EFF485FA9DBB914A40C96` | `FD4282931F83021EC570C2B468DF7D0AAC953A9D0B0859CA64B406FC15C74901` | `58615189E9B9E720BC732F4E983A21AD4AF8857667F378F0C87E15CDB4E0F4B7` |
| 条目数 | 333 | 333 | 333 |
| 可解压 | 是 | 是 | 是（已解压至 `%TEMP%\dlr_v100_r3zip`） |
| `DouyinLiveRecorder.exe` 版本 | 1.0.0.0 / ProductVersion 1.0.0 | 1.0.0.0 / 1.0.0 | 1.0.0.0 / 1.0.0 |
| 内置 FFmpeg | 存在 | 存在 | 存在 |
| 根目录许可 | 齐全 | 齐全 | 齐全 |

| 检查项 | 结果 | 状态 |
| --- | --- | --- |
| 原始包哈希核对 | 与任务书一致 | PASS |
| 原始包冻结 | 已设为只读，并在备份中保留副本；未被覆盖 | PASS |
| r3 包内容完整性 | 333 条目、关键二进制与许可齐全、可解压、11 步 E2E 通过 | PASS |

## 5. 真实直播测试

| 项目 | 结论 | 状态 |
| --- | --- | --- |
| 开播检测 → 自动录制 → 文件生成 → 元数据 → 归档 → 状态 → 统计 | 未执行真实抖音直播 | **BLOCKED** |

原因：本次审计未获得任何明确授权的在播直播间/账号/Cookie。按授权边界，禁止使用未授权真实账号或直播间，因此该阶段整体 BLOCKED，而非 PASS 或 FAIL。现有替代证据仅为 Mock 合成流端到端（§6、§15），不构成真实直播证据。

## 6. 异常恢复测试（打包候选 r2，Mock 合成流）

执行脚本：`%TEMP%\e2e_v100_r2.ps1`（11 步 UI 自动化）与 `%TEMP%\dlr_v100_crash2.ps1`（崩溃/断网专项）。

| 场景 | 实测结果 | 状态 |
| --- | --- | --- |
| 开播检测与自动录制 | 监控启动后 1 个 FFmpeg，文件增长至 524,288 字节 | PASS |
| 强杀 FFmpeg | 自动续录，出现第 2 个分段；无残留 | PASS |
| 应用意外退出（强杀主进程） | 重启后遗留录制标记 `interrupted`（保留 524,288 字节），监控会话 `interrupted` | PASS |
| 崩溃时 FFmpeg 进程 | 父进程被强杀后 FFmpeg 因 stdin 关闭自行退出，实测遗留 0 个 | PASS（清理注册表路径由 `test_process_registry` 覆盖） |
| 重启后状态恢复 | 录制历史保留、无重复记录，退出码 0 | PASS |
| 重复任务防护 | 连续两次“开始监控”后 FFmpeg 数仍为 1，数据库仅 1 条 `recording` | PASS |
| 网络中断（停止合成流服务） | FFmpeg 退出，失败分段落库，自动重试（1/5…），无残留 | PASS |
| 网络恢复 | 服务恢复后第 3 次重试成功续录，新增分段最终 `completed` | PASS |
| 进程清理/退出 | 停止录制后 FFmpeg 0 个；关窗退出码 0，无残留 | PASS |
| 重复归档/文件损坏 | 归档幂等与“校验后删源”由 `test_archive_service` 10 项覆盖（§7） | PASS（逻辑层） |

说明：网络中断重试会产生 0 字节失败分段记录，属失败历史留痕，不是重复录制；按 P3 观察项记录，未改动行为。

## 7. 跨物理磁盘归档与磁盘空间保护

### 7.1 跨物理磁盘

| 检查项 | 结果 | 状态 |
| --- | --- | --- |
| 真实第二块物理磁盘 | 本机仅 1 块 NVMe（`Get-Disk`：磁盘 0，1 TB，GPT）；无 D: 等其它卷 | **BLOCKED** |
| 管理员挂载 VHD 模拟新卷 | 当前进程 `IsAdmin=False`，不可用 | **BLOCKED** |

未使用 `subst` 或同盘双目录冒充跨盘证据。

### 7.2 归档逻辑（同卷，自动化证据）

`tests/test_archive_service.py`、`test_db_archive.py`、`test_archive_rules.py`、`test_safe_fileops.py`、`test_disk_protection.py` 共 **46 passed**，覆盖：

| 能力 | 覆盖测试 | 状态 |
| --- | --- | --- |
| 仅在校验后删除源文件 | `test_delete_source_only_after_verification` | PASS |
| 同名冲突 `_1.._n` 不覆盖 | `test_conflicting_target_gets_suffix_without_overwrite` | PASS |
| 同内容去重跳过 | `test_identical_target_is_skipped_without_copy` | PASS |
| 录制中文件不处理 | `test_recording_in_progress_is_never_touched` | PASS |
| 目标空间不足保留源文件 | `test_low_disk_space_marks_failed_and_keeps_source` | PASS |
| 中断后恢复重试 | `test_recover_pending_retries_interrupted_archive` | PASS |
| 重复入队拒绝 | `test_duplicate_enqueue_is_rejected_while_queued` | PASS |
| 归档默认关闭/状态 none | `test_archive_disabled_leaves_status_none` | PASS |

### 7.3 磁盘空间保护

| 检查项 | 结果 | 状态 |
| --- | --- | --- |
| 低于阈值阻止启动录制 | `test_disk_protection.py::test_start_is_blocked_when_disk_space_is_low` 通过 | PASS |
| 录制中磁盘不足的保护/停止 | 由录制服务低空间处理测试覆盖 | PASS |
| 空间恢复后恢复任务 | 由阈值/预警状态测试覆盖 | PASS |
| 真实磁盘写满 | 未执行（不得删除用户视频腾空间，也不应人为写满系统盘） | **NOT TESTED** |

## 8. 24 小时稳定性测试

| 项目 | 结果 | 状态 |
| --- | --- | --- |
| 24 小时完整窗口 | 未达到 24 小时 | **NOT TESTED** |

过程如实记录：

1. 首次观察（原始候选）：2026-11-28 15:29:25 启动，1 房间、Mock 合成流、自动录制。15:31:51 被本轮崩溃恢复测试的进程清理逻辑误杀（同一临时根目录匹配），实际观察 120 秒。中断原因属于审计工具干扰，不代表被测版本异常。
2. 正式观察（r2 候选，运行时与 r3 仅差一个窗口最小尺寸常量）：2026-11-28 15:53:19 启动，1 房间、Mock 合成流、自动录制。16:12:51 由审计人员为解除单实例占用而强制结束，累计 **1146 秒（19.1 分钟）**、18 次 60 秒采样。结果：
   - RSS：186.5 MB → 131.9 MB（其中一次 GC 回落，随后稳定在 131.9–138.3 MB，无持续增长）；
   - CPU 累计约 6.2 s（采样周期内增量 0–4 s）；
   - FFmpeg 子进程全程 1 个，线程数 20–29；
   - 录制文件从 0 增长到 30,146,560 字节，无停滞；
   - 每 10 分钟 `PRAGMA integrity_check=ok`，Schema=4；
   - 强制结束应用后录制子进程自行退出（AFTER_STOP ffmpeg_left=0）。
3. 未执行完整 24 小时窗口，故本项不判 PASS。

证据：`%TEMP%\dlr_v100_soak_r2.log`（18 条 SAMPLE 记录）。

不将短时观察写成 24 小时 PASS。

## 9. Windows 兼容性与路径测试

| 项目 | 结果 | 状态 |
| --- | --- | --- |
| Windows 11 | 全部测试与打包运行实测 | PASS |
| Windows 10 | 无真实环境 | **NOT TESTED** |
| 非管理员权限 | 当前会话 `IsAdmin=False`，全部测试/打包/E2E 均通过 | PASS |
| 中文 + 空格路径 | `%TEMP%\审核 中文目录\数据 目录\v100`：DB 与日志创建成功，进程正常运行 | PASS |
| 长路径（231 字符） | `%TEMP%\longpath_test\abcdefghij…×18\数据`：DB 与日志创建成功 | PASS |
| 分辨率 1920×1080 / 1280×720 / 1024×640 | 页面布局截图走查（§10） | PASS |
| 900×600 强制缩放 | 原最小尺寸 900×600 会让统计卡片与页头按钮被裁剪；已将最小窗口提升到 1024×640，强制 `resize(900,600)` 会钳制为 1024×640，实测无裁剪 | PASS（修复后） |

## 10. UI 人工走查

方法：以原生 Windows 平台创建主窗口并移到屏幕外，逐页 `grab()` 截图；覆盖 7 个页面 × 浅色/深色 × 1280/1920，另加 1024 与“添加直播间”弹窗。

| 页面/状态 | 结果 | 状态 |
| --- | --- | --- |
| 直播间 / 录制历史 / 运行日志 / 设置 | 布局正常；日志自动换行；设置页滚动区正常 | PASS |
| 媒体库（1280） | **发现横向溢出与“大小”列截断**，已修复并复核 | PASS（修复后） |
| 统计（1280/1024） | **发现“按主播统计”容量/时长列被右侧面板截断**，已修复并复核 | PASS（修复后） |
| 自动化 | 布局正常；无规则时状态行显示“共 0 条规则，0 条执行记录” | PASS |
| 弹窗（添加直播间） | 浅/深色字段、按钮、提示正常 | PASS |
| 空数据/加载/错误 | 列表空态由状态行提示；统计加载期显示“正在统计…”；错误经日志页与消息框呈现 | PASS |
| 1024 宽度媒体库 | 表格保留横向滚动（数据表合理降级），无控件错位 | PASS |
| 900×600 最小窗口 | 强制缩放到 900×600 时统计卡片/页头按钮被裁剪，已定位并修复：最小窗口提升为 1024×640，1024 实测完整 | PASS（修复后） |
| 多显示器/高 DPI 真实视觉 | 未在真实多显示器与大字号缩放下人工复核 | **NOT TESTED** |

截图位置：`%TEMP%\dlr_ui_shots_c\`（修复后）与 `%TEMP%\dlr_ui_shots_b\`（修复前对照）。

## 11. 远端 CI

| 提交 | 工作流 | 结果 | 状态 |
| --- | --- | --- | --- |
| `65bc3d5` | CI / pytest (Windows, Python 3.12) | run `46121491127` **success** | PASS |
| `8ec6c55` | 同上 | run `46123258969` **success** | PASS |
| `b348ab9`（最小窗口修复，构建基线） | 同上 | run `46124941889` **success** | PASS |
| `fbc09af`（审计报告提交，当前 HEAD） | 同上 | run `46125040429` **success** | PASS |

未删除测试、未降低断言、未跳过用例；未强制推送，未覆盖他人提交。

## 12. 全量回归

| 轮次 | 命令 | 结果 |
| --- | --- | --- |
| 修复前 | `.venv\Scripts\python.exe -m pytest -q` | **566 passed / 0 failed / 0 skipped**，302.89s |
| UI 溢出修复后 | 同上 | **566 passed / 0 failed / 0 skipped**，276.66s |
| 最小窗口修复后 | 同上 | **566 passed / 0 failed / 0 skipped**，288.50s |

覆盖：数据库与迁移（V3→V4、V1→V4、失败回滚、幂等）、备份与恢复、归档、解析契约/重试/熔断、录制服务与指标、媒体探测、资产组织、规则引擎、自动化页面、统计、更新检查、单实例、性能预算、GUI 核心交互。

## 13. 已修复问题

| # | 等级 | 表现 | 复现条件 | 根因 | 修改 | 测试与回归 | 提交 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | P2 | 统计页“按主播统计”表格右侧容量/最近录制列被截断 | 1280×720、1024×640；主播名较长 | 所有列 `ResizeToContents` + 长文本列撑宽，超出左右分栏可用宽度 | `_make_table` 新增 `stretch_column`，长文本列改为 Stretch + 右侧省略；失败原因表同样处理 | 截图复核（1280/1024，浅/深色）；566 passed | `8ec6c55` |
| 2 | P2 | 媒体库表格在 1280 出现横向滚动，“大小”列被截断 | 1280×720，存在长主播名/文件路径 | 同上，且右侧详情面板占用 232–300px | 主播列与文件路径列改为 Stretch + 省略，取消强制末列拉伸 | 截图复核；566 passed | `8ec6c55` |
| 3 | P2 | 窗口可缩到 900×600，统计卡片与页头按钮被裁剪 | 将窗口拖到 900 宽或程序化 `resize(900,600)` | 最小尺寸 900×600 小于布局实际最小宽度（约 1100），显式 minimumSize 压过了布局最小提示 | `main_window` 最小窗口改为 1024×640；程序化缩到 900 时被钳制，1024 下卡片/按钮完整 | 截图复核 1024×640；全量回归 566 passed | `b348ab9` |
| 4 | P3 | 断网重试会留下 0 字节失败分段记录 | 网络中断触发重试 | 每次尝试按设计创建分段记录 | 评估为失败历史留痕，未改动 | — | — |

未发现 P0/P1 缺陷。审计期间未修改任何用户数据或凭据。

## 14. 未解决风险

| 等级 | 项目 | 状态 |
| --- | --- | --- |
| 高 | 真实抖音直播录制（风控、接口变更、清晰度） | **BLOCKED**（无授权直播间） |
| 中 | 24 小时长稳 | **NOT TESTED**（观察未满 24h） |
| 中 | 跨物理磁盘归档真机验证 | **BLOCKED**（仅 1 块磁盘、无管理员） |
| 低 | Windows 10、低配机型 | **NOT TESTED** |
| 低 | 真实更新清单与升级回滚演练 | **NOT TESTED** |
| 低 | 多显示器 / 高 DPI / 大字号视觉复核 | **NOT TESTED** |
| 信息 | 网络重试的 0 字节失败分段 | 设计留痕，P3 观察项 |

## 15. 最终构建信息

| 项目 | 值 |
| --- | --- |
| 构建命令 | `.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --distpath dist\v1.0.0-r3 --workpath build\_work_v100_r3 build\pyinstaller.spec` |
| 打包方式 | PyInstaller onedir；随后补入根目录 `LICENSE`、`THIRD_PARTY_NOTICES.txt`、`LICENSES/`，`Compress-Archive` 生成 ZIP |
| 构建产物 | `dist\v1.0.0-r3\DouyinLiveRecorder\`（`DouyinLiveRecorder.exe` 5,733,586 字节） |
| **最终候选 ZIP** | `dist\DouyinLiveRecorder-v1.0.0-candidate-r3.zip` |
| 大小 | 109,699,573 字节 |
| SHA-256 | `58615189E9B9E720BC732F4E983A21AD4AF8857667F378F0C87E15CDB4E0F4B7` |
| 中间候选 r2（已被取代） | 109,700,554 字节，`FD4282931F83021EC570C2B468DF7D0AAC953A9D0B0859CA64B406FC15C74901` |
| 原始候选 | 保留且只读：`B59250227EBE51D7CBF283121F6555416FB3BB78913EFF485FA9DBB914A40C96` |
| 候选端到端 | 对 r3 ZIP 解压副本执行 11 步闭环：强杀 FFmpeg 后续录 2 段、重启保留 2 段、两次退出码均为 0、无残留 |
| 当前 HEAD | `fbc09af`（审计报告提交；构建基线 `b348ab9`） |
| 补充备份 | `DouyinLiveRecorder-Backups\v1.0.0_b348ab9_final_20261128_161628\`（含 `fbc09af` bundle、跟踪源码、文档、r3 候选包、12 项 SHA-256 清单与 RESTORE.md，bundle 校验通过；报告定稿后再次刷新文档快照） |

## 16. 发布建议

| 阶段 | 结论 |
| --- | --- |
| 开发完成 | 是 |
| 候选构建完成 | 是（r3，含哈希、许可证与端到端闭环） |
| 专项测试完成 | 部分：可本地执行的项目全部完成；真实直播、24h、跨物理磁盘受限 |
| 发布审计通过 | **否**（条件性通过：CONDITIONAL PASS） |
| 正式发布完成 | **否**（未获授权，未合并、未打标签、未上传） |

**建议：CONDITIONAL PASS。** v1.0.0 在本地可验证范围内达到候选发布质量：566 项回归通过、打包端到端与异常恢复通过、备份可恢复、UI 溢出已修复、Windows 11/非管理员/中文与长路径可用。正式发布前仍需：

1. 使用用户明确授权的真实直播间完成 §5 的录制全链路验证；
2. 在具备第二块物理磁盘的机器上完成跨盘归档验证（或在授权下挂载 VHD 并记录为虚拟卷证据）；
3. 完成完整 24 小时稳定性观察并附采样日志；
4. 由用户明确授权后再执行合并、打标签与公开发布；在此之前保持候选状态。

远端 CI 已全部通过（`65bc3d5` / `8ec6c55` / `b348ab9` / `fbc09af`，见 §11），不再作为发布前待办。

---

## 附录：原始安装包与候选包（历史参考）

| 版本 | 文件 | 大小 | SHA-256 |
| --- | --- | --- | --- |
| v0.5.0 | `DouyinLiveRecorder-v0.5.0-candidate.zip` | 109,217,601 | `B51AB48EAC2C77F7F0525CD518D8275EE2C0875C798F141BC916D2501D60C697` |
| v1.0.0 原始 | `DouyinLiveRecorder-v1.0.0-candidate.zip` | 109,699,513 | `B59250227EBE51D7CBF283121F6555416FB3BB78913EFF485FA9DBB914A40C96` |
| v1.0.0 r2 | `DouyinLiveRecorder-v1.0.0-candidate-r2.zip` | 109,700,554 | `FD4282931F83021EC570C2B468DF7D0AAC953A9D0B0859CA64B406FC15C74901` |
| v1.0.0 r3（最终） | `DouyinLiveRecorder-v1.0.0-candidate-r3.zip` | 109,699,573 | `58615189E9B9E720BC732F4E983A21AD4AF8857667F378F0C87E15CDB4E0F4B7` |

## 附录：证据文件位置（本机临时目录，不属于交付包）

| 证据 | 路径 |
| --- | --- |
| 原始候选 E2E | `%TEMP%\e2e_v100_audit.log` |
| r2 候选 E2E | `%TEMP%\e2e_v100_r2.ps1`（输出见执行记录） |
| 崩溃/断网专项 | `%TEMP%\dlr_v100_crash2.log` 与 `%TEMP%\dlr_v100_releaseaudit\crash2\data\logs\` |
| 回归（修复前/后） | `%TEMP%\pytest_final_audit.log`、`%TEMP%\pytest_postui.log` |
| 稳定性观察 | `%TEMP%\dlr_v100_soak_r2.log` |
| UI 截图 | `%TEMP%\dlr_ui_shots_b\`（修复前）、`%TEMP%\dlr_ui_shots_c\`（修复后） |
| 备份与恢复说明 | 备份目录 `RESTORE.md`、`SHA256SUMS.txt` |
