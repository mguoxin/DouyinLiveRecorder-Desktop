# DouyinLiveRecorder-Desktop 0.3.0 交付说明

- 版本：0.3.0（基线 `v0.2.0`）
- 日期：2026-11-18
- 范围：批次 F（FFmpeg 进程生命周期与异常恢复）、G（清晰度与 Cookie 登录配置）、H（系统稳定性与兼容性验证）
- 详细测试记录：[TEST_REPORT_0.3.0.md](TEST_REPORT_0.3.0.md)

## 1. 本版本完成内容

### 1.1 批次 F：进程生命周期

- 新增 `src/dlrecorder/infrastructure/winproc.py`：查询进程创建时间与镜像路径、判断存活、终止进程、
  Windows Job Object（`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`）。
- 新增 `src/dlrecorder/infrastructure/process_registry.py`：原子写入的进程登记表
  `data/runtime/ffmpeg_processes.json`，记录 PID、创建时间、可执行文件路径、recording_id、room_id、文件路径。
- 新增 `src/dlrecorder/infrastructure/orphan.py`：遗留进程归属核验与清理报告。
- `RecordingService` 每个分段启动后登记，结束/失败后注销；应用启动时先清理遗留进程，再恢复中断记录；
  `shutdown()` 最后关闭 Job。
- 安全策略：三项身份（PID + 创建时间 + 路径）必须同时匹配才会终止；无法确认时跳过并保留登记，
  不做“扫描所有 FFmpeg 统一结束”。

### 1.2 批次 G：清晰度与 Cookie

- 新增 `src/dlrecorder/infrastructure/credential_store.py`：Windows DPAPI 加密保存 Cookie
  （`data/runtime/douyin_cookie.bin`，magic `DLR1`，附加 entropy，禁止 UI 提示）。
- `SettingsService`：新增 `recording.quality`（默认 `UHD`）与 Cookie 的保存/读取/清除/状态查询；
  Cookie 不写入 `settings` 表。
- 设置界面：清晰度下拉框（OD/UHD/HD/SD/LD）、Cookie 密码输入框（不回显）、导入/显示明文/清除按钮、
  Cookie 状态标签与说明文字。
- 解析器：`EngineDouyinParser` 支持 `set_quality()` / `set_cookie()` 与 `cookie_provider` 按需读取；
  Cookie 读取失败降级为匿名；风控且已配置 Cookie 时提示检查 Cookie 是否失效。
- 清晰度修改对下一次解析生效，不影响正在进行的录制；无可用档位时由上游映射自动回退。

### 1.3 批次 H：稳定性修复

- 网络中断恢复：FFmpeg 退出码 0 但重新解析无法确认直播状态（典型为断网造成的提前 EOF）时，
  不再直接按“已完成”收尾，而是按异常重试续录；明确已下播仍按完成收尾。
- 输出长时间不增长（默认 120 s）：确认进程退出后结束该分段并续录，而不是终止整场录制；
  进程未确认退出时仍按失败收尾，避免同一路径被两个进程写入。
- 重试预算统一为 5 次、退避 2/5/10/20/30 秒，超过后如实标记失败，不会无限重连。

## 2. 架构与配置变更

| 项目 | 变更 |
| --- | --- |
| 新增模块 | `infrastructure/winproc.py`、`infrastructure/process_registry.py`、`infrastructure/orphan.py`、`infrastructure/credential_store.py` |
| 新增运行时文件 | `data/runtime/ffmpeg_processes.json`（进程登记）、`data/runtime/douyin_cookie.bin`（DPAPI 凭据） |
| 新增设置项 | `settings.recording.quality`（OD/UHD/HD/SD/LD，默认 UHD） |
| 数据库 Schema | **未变更**（V2，无迁移） |
| 录制分段 | 同一场直播在网络中断/进程异常后续录到新文件，一个直播场次可对应多条 `recordings` 记录 |
| 退出语义 | 退出码 0 需重新解析确认；无法确认时按异常续录（受预算约束） |

## 3. 兼容性与数据迁移

- 从 0.2.0 升级：直接替换程序目录即可，`data/app.db` 无需迁移；`data/runtime/` 会在首次启动时自动创建。
- 降级回 0.2.0：0.2.0 不识别 `data/runtime/`，不影响启动；建议先停止录制。
- 旧版本遗留的 `recording` 记录仍按原有逻辑标记 `interrupted` 并回填实际文件大小。
- Cookie 不出现在数据库、配置表、日志与发布包中；发布包不包含 `data/` 目录。
- 已知迁移风险：升级瞬间若仍有 0.2.0 进程在录制，0.3.0 启动清理只识别本版本登记的进程，
  不会终止旧进程；请先关闭旧程序。

## 4. 验证摘要

| 验证 | 结果 |
| --- | --- |
| 自动化回归 | 259 passed |
| F：崩溃/强杀/重启/遗留进程/PID 重用/不误杀 | 通过（真实进程验证） |
| G：五档清晰度真实解析、HD 真实录制、Cookie DPAPI 加密与失效风控 | 通过 |
| H：4 房间并发 30 分钟、单任务被杀不影响其他、网络 8 s/长中断/坏地址 | 通过 |
| H：低配模拟（2 核亲和性） | 通过（模拟，非 i3/4GB 真机） |
| 打包版端到端 | 通过（真实直播间，见第 5 节与测试报告 4.6） |

## 5. 发布包与校验

| 项目 | 值 |
| --- | --- |
| 构建 | PyInstaller 6.22.3，onedir，入口 `build/entry.py`，规格 `build/pyinstaller.spec` |
| 构建时间 | 2026-11-18 19:52（本机时间） |
| 可执行文件 | `DouyinLiveRecorder.exe`，5,497,269 B |
| 发布目录 | `release\DouyinLiveRecorder-0.3.0-win64\`（174 个文件，解压后 201.97 MB） |
| 压缩包 | `release\DouyinLiveRecorder-0.3.0-win64.zip`，81,497,706 B |
| SHA-256 | `D2891DFD553747B7FDD795E7598D720DB4FABED443088056E0D45E820138D883` |
| 校验文件 | `release\SHA256SUMS.txt`（追加 0.3.0 条目，沿用「大写哈希 + 两个空格 + 文件名」格式） |

发布包内容：

- `DouyinLiveRecorder.exe`
- `_internal\`（Python 运行时、PySide6、内置 FFmpeg 9.0.2 / ffmpeg.exe）
- `README.md`、`THIRD_PARTY_NOTICES.txt`、`licenses\FFmpeg-LICENSE.txt`

发布包**不含**：`data\` 目录（数据库、日志、录制文件）、`runtime\douyin_cookie.bin`、任何 `.ts/.flv` 录制文件。
已用脚本检查 zip 内 174 个条目，命中 `data/`、`douyin_cookie`、`app.db`、`.ts/.flv` 的条目数为 **0**。

打包版端到端验证（真实直播间，UIA 驱动真实窗口，隔离数据目录）：添加房间 → 开始监控（LIVE）→ 自动录制
（127,096,272 B，1088x1920 H.264 + AAC，解码 0 错误）→ 设置界面清晰度「超清 (UHD)」与 Cookie 状态 →
Cookie 保存（DPAPI 密文 282 B，日志仅记录长度）→ Cookie 清除 → 停止录制（`completed`，DB 与磁盘一致）→
正常关闭（退出码 0，无残留 FFmpeg，登记表为空）。逐步记录见 `docs/TEST_REPORT_0.3.0.md` 第 4.6 节。

## 6. 已知问题与剩余风险

| 编号 | 问题 | 影响 | 处理 |
| --- | --- | --- | --- |
| R1 | 未在 i3/4GB 真机验收 | 低配指标仅有 2 核亲和性模拟数据 | 沿用 `tools\acceptance_low_spec.ps1` 在目标机器执行 |
| R2 | 长时验证为 30 分钟 × 4 房间 | 未覆盖 24 小时录制趋势 | 建议后续安排 24h 录制采样 |
| R3 | 直播结束后退出码 0 也需重新解析确认 | 解析接口不可用时最多延迟约 67 s 才收尾 | 属准确性取舍，已受重试预算约束 |
| R4 | 解析依赖抖音接口与签名规则 | 接口变更需同步上游引擎版本 | 回退解析器仍可用；上游 MIT 项目持续维护 |
| R5 | Cookie 仅当前 Windows 用户可解密 | 换用户/换机器需重新输入 | 设计使然；界面显示“未配置” |
| R6 | 断网超过 `-rw_timeout`（15 s）时会出现分段切换 | 同一场直播产生多个文件 | 文件完整可解码，历史中可见多条记录 |
| R7 | 未遇到“目标清晰度缺失”的真实房间 | 降级逻辑仅由上游映射与单测覆盖 | 后续真实环境遇到时补充验证 |

## 7. 回滚

- 保留 `release\DouyinLiveRecorder-0.2.0-win64.zip` 与标签 `v0.2.0`；回滚时替换程序目录即可。
- 回滚不会修改或删除已有 `data/app.db`、录制文件与历史记录。

## 8. Git 记录与标签

（打标签后填写：提交列表、标签、工作区状态）
