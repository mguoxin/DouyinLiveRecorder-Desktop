# DouyinLiveRecorder-Desktop 0.1.0 发布报告

- 发布日期：2026-11-18
- 发布版本：**0.1.0**
- 发布标签：**v0.1.0**
- 发布状态：**已发布（低配机器验收按所有者决定暂缓，未验证）**
- 适用平台：Windows 10 / 11 x64（onedir 便携版）

## 1. 发布概要

| 项目 | 内容 |
| --- | --- |
| 源码提交 | `v0.1.0` 标签指向的提交，可用 `git rev-parse v0.1.0^{commit}` 查询 |
| 分支 | `master` |
| 构建时间 | 2026-11-18 00:12:43 |
| 构建环境 | Python 3.12.16 / PySide6 6.12.0 / PyInstaller 6.22.3 |
| 内置 FFmpeg | 9.0.2-essentials_build-www.gyan.dev（GPL v3） |
| 完整回归 | **205 passed / 0 failed** |
| 发布目录 | `release\DouyinLiveRecorder-0.1.0-win64\`（199.07MB，172 个文件） |
| 发布压缩包 | `release\DouyinLiveRecorder-0.1.0-win64.zip`（74.95MB） |
| ZIP SHA-256 | `8FF1AAED738CE215B00ADC152B4CF893DB2FD7F8AA820C8FF48C9031C1848AAF` |
| FFmpeg SHA-256 | `3256173F3F8BFFD7DF12227C68ADF68025EDB1832273A9530688A7BB1ED8EDEC` |

校验和也可在 `release\SHA256SUMS.txt` 中核对。

## 2. Git 源码基线

- 已在项目根目录初始化 Git 仓库，初始化前确认当前目录及上级目录均无既有仓库，未覆盖任何历史。
- 首个提交为 0.1.0 交付基线，包含源码、测试、构建脚本、文档、验收工具与 FFmpeg 许可文件。
- 发布标签 `v0.1.0` 为附注标签，指向包含本报告的发布提交。
- `.gitignore` 排除以下内容，避免把环境、产物与用户数据纳入版本库：
  - 运行环境：`.venv/`、`.venv-acceptance/`、`__pycache__/`、`.pytest_cache/`
  - 构建/发布产物：`dist/`、`release/`、`build/pyinstaller/`
  - 内置 FFmpeg 可执行文件：`tools/ffmpeg/bin/*.exe`（约 100MB，版本与校验和见本报告）
  - 用户数据：`data/`（数据库、录制文件、日志）
  - 验收/测试产物：`acceptance/`、`_test_artifacts/`、`.screenshots/`、`_probe_gui/`

复现构建：

```powershell
.venv\Scripts\python.exe -m pytest -q
powershell -NoProfile -ExecutionPolicy Bypass -File build\build.ps1
```

## 3. 测试结果

- 最终完整回归：**205 passed / 0 failed**（离线，30.54s）。
- 基线 197 项全部保留并通过；本阶段新增 8 项，覆盖：
  1. 打包数据目录定位（EXE 同级 / `DLR_DATA_DIR` / 源码 cwd / 实际应用落地）；
  2. 启动失败提示与退出码 4；
  3. 监控按钮以后台真实状态为准；
  4. 无法确认 FFmpeg 退出时不得标记 `completed`；
  5. WAL 存在时数据库安全副本包含已提交数据。
- 按所有者要求，未重复 24 小时稳定性采样与真实抖音录制测试；两者此前的通过记录仍然有效，
  本报告不把它们计为本次重新验证。

## 4. 构建与产物

构建命令：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File build\build.ps1
```

产物与一致性：

- 程序：`dist\DouyinLiveRecorder\DouyinLiveRecorder.exe`（onedir，无控制台窗口）。
- 所有源码修改时间均早于正式构建时间，构建内容与 `v0.1.0` 源码一致。
- 窗口标题与启动日志均显示 `0.1.0`，可直接核对运行版本。
- 发布包目录结构：

```
DouyinLiveRecorder-0.1.0-win64\
  DouyinLiveRecorder.exe
  _internal\                     # Qt、Python 运行时、内置 FFmpeg
  README.md
  THIRD_PARTY_NOTICES.txt
  licenses\FFmpeg-LICENSE.txt
  licenses\FFmpeg-README.txt
```

- 发布包**不包含**：`.venv`、测试代码、验收脚本、数据库、录制文件、日志、开发探测环境及其他测试产物。

## 5. 发布验收结果

| 验收项 | 方法 | 结果 |
| --- | --- | --- |
| 自动化回归 | 完整 pytest | **205 passed** |
| 版本一致性 | 窗口标题 / 启动日志 / `__init__.py` / `pyproject.toml` | 一致（0.1.0） |
| 正式构建可启动 | 发布目录直接启动 12s 后关闭 | 退出码 **0** |
| 干净环境启动 | 复制发布目录到 `%TEMP%`，从 `%TEMP%` 作为 cwd 启动 | 退出码 **0** |
| 数据目录定位 | 未设置 `DLR_DATA_DIR` 时从其他 cwd 启动 | 数据创建在 EXE 同级 `data\`，cwd 无数据 |
| 首启初始化 | 空目录启动 | 自动创建 `data\app.db` 与 `data\logs\app.log` |
| 内置 FFmpeg | 启动日志与文件存在性 | 自动使用 `_internal\ffmpeg\bin\ffmpeg.exe` |
| GUI 冒烟 | `acceptance_low_spec.ps1 -Mode gui`（40s / 5s） | 8 条采样、峰值 **96.57MB**、增长 **2.56MB**、CPU 平均 **0.72%**、退出码 **0**，判定通过 |
| 单实例 | 自动化测试 + 命名互斥量 | 通过 |
| 关闭流程 | 主窗口关闭、线程/进程/DB 收尾 | 退出码 0，日志无异常 |
| 数据隔离 | 验收期间 `DLR_DATA_DIR` 指向临时目录 | 未读写用户数据 |
| 发布包内容 | 检查数据库、日志、开发产物 | 均不存在 |
| 敏感信息 | 检索源码与入库文件 | 未发现凭据、Cookie、Token、密钥 |

GUI 验收 JSON：`acceptance\acceptance-20261118_001354.json`。

说明：以上 GUI 与启动数据来自本开发机（Core Ultra 5 125H / 18 线程 / 31.47GB），
**不是低配机器验收结论**。

## 6. 数据安全检查

- 发布包不包含 `data\`，首次运行自动初始化空数据库，不携带任何用户数据。
- Git 仓库不跟踪 `data\`、录制文件、日志、备份与验收产物。
- 测试与验收全部使用临时目录或 `DLR_DATA_DIR`，未修改正式数据库。
- 备份使用 SQLite Backup API，恢复前生成校验过的安全副本；恢复失败不替换当前数据库。
- 数据库 Schema V2 未作任何不可逆迁移；删除房间只置空关联，保留历史快照与录制文件。
- 未发现凭据、Cookie、Token、API Key 等敏感内容被提交或打包。

## 7. 已知限制与未验证项

1. **低配机器（i3 / 4GB）验收未执行**：所有者明确暂缓，不纳入本次发布阻断条件；
   该项仍为“未验证”，不得视为通过。目标机器可按 `docs\PROJECT_STATUS.md` 第 8 节执行
   `tools\acceptance_low_spec.ps1`。
2. **未做代码签名**：Windows SmartScreen 可能对首次运行给出未知发布者提示。
3. **许可合规**：FFmpeg 为 GPL v3 构建，PySide6/Qt 为 LGPLv3；包内已附许可指引与 FFmpeg 许可文本，
   正式对外大规模分发前建议由所有者完成法务复核。
4. **恢复策略残余风险**：当前数据库已损坏且存在恢复请求时，安全副本校验失败会中止恢复
   （保持现场、不替换）。这是有意采取的安全策略，尚未改为“损坏库改名后继续恢复”。
5. **自动录制语义**：自动录制开启时，直播中手动停止录制会在下一个监控周期再次开始；
   需要长期停录请关闭自动录制或停止监控。
6. **解析依赖公开页结构**：抖音公开直播页 React Flight 结构变化时可能需要同步维护解析器；
   无效房间统一按 `API_MALFORMED` 处理，不会误开录制。
7. 本版本不支持登录态/Cookie 录制，仅使用匿名会话访问公开页面。

## 8. 发布检查清单

- [x] 自动化测试全部通过（205 passed）
- [x] 正式构建与源码版本一致
- [x] EXE 可在干净环境启动
- [x] 内置 FFmpeg 可正常调用
- [x] 数据目录定位正确（EXE 同级，`DLR_DATA_DIR` 可覆盖）
- [x] 启动失败有明确提示（退出码 4）
- [x] 单实例机制正常
- [x] 关闭流程正常、退出码 0
- [x] 用户数据不因启动位置变化而丢失或隐藏
- [x] 发布包不含开发环境、测试产物与敏感文件
- [x] README、版本号与变更记录一致
- [x] Git 仓库、提交与 `v0.1.0` 标签已建立
- [ ] 低配机器（i3 / 4GB）真机验收（所有者暂缓，未验证）

## 9. 回滚与支持

- 发布包与源码标签 `v0.1.0` 保持一致；如需回退，可直接使用 `release\DouyinLiveRecorder-0.1.0-win64.zip`。
- 升级或回退前，建议用程序内「备份数据」生成一致性备份，备份默认位于 `data\backups\`。
- 运行数据始终保存在程序同级的 `data\`；替换程序目录时请保留该目录。
- 问题排查顺序：`data\logs\app.log` → 「设置」中 FFmpeg 检测 → 恢复备份 → 核对版本号。
