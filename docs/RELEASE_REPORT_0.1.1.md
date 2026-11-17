# DouyinLiveRecorder-Desktop 0.1.1 发布报告

- 发布日期：2026-11-18
- 发布版本：**0.1.1**
- 发布标签：**v0.1.1**
- 发布类型：缺陷修复版本（兼容 0.1.0 数据目录与数据库，无 Schema 变更）
- 发布状态：**已发布（低配机器验收仍未执行，沿用 0.1.0 的未验证结论）**
- 适用平台：Windows 10 / 11 x64（onedir 便携版）

## 1. 发布概要

| 项目 | 内容 |
| --- | --- |
| 源码提交 | `v0.1.1` 标签指向的提交，可用 `git rev-parse v0.1.1^{commit}` 查询 |
| 分支 | `master` |
| 构建时间 | 2026-11-18 01:18:26 |
| 构建环境 | Python 3.12.16 / PySide6 6.12.0 / PyInstaller 6.22.3 |
| 内置 FFmpeg | 9.0.2-essentials_build-www.gyan.dev（GPL v3，未变更） |
| 完整回归 | **209 passed / 0 failed** |
| 发布目录 | `release\DouyinLiveRecorder-0.1.1-win64\`（199.07MB，172 个文件） |
| 发布压缩包 | `release\DouyinLiveRecorder-0.1.1-win64.zip`（78,596,201 字节 / 74.95MB） |
| ZIP SHA-256 | `758DA354FA3FA8E0ED2C454065A1FDF724F91F50E60184C354A507D7A5DFA72A` |
| FFmpeg SHA-256 | `3256173F3F8BFFD7DF12227C68ADF68025EDB1832273A9530688A7BB1ED8EDEC` |

校验和也可在 `release\SHA256SUMS.txt` 中核对；0.1.0 的 ZIP 记录同时保留。

## 2. 缺陷与修复内容

### 2.1 用户报告

用户反馈“几次输入直播间链接都无法正常读取”。发布版日志中对应 6 条同源告警：

```
[WARNING] dlrecorder.application.monitor_service: 添加房间参数非法:
直播间 ID 只能包含字母、数字、下划线、点和短横线，长度 1-64
```

### 2.2 根因

0.1.0 的 `validate_web_rid` 只接受纯 `web_rid`（如 `95694179922`），界面提示也只写“直播间 ID”。
用户粘贴完整直播间链接（`https://live.douyin.com/...`）时，`:`、`/` 不在允许字符内，
请求在进入解析器之前即被拒绝，表现为“无法读取”。

### 2.3 修复

- `src/dlrecorder/domain/models.py`：`validate_web_rid` 支持从输入中自动提取 `web_rid`
  - `https://live.douyin.com/<数字>`（含查询参数、末尾斜杠）
  - `https://www.douyin.com/root/live/<数字>`、`https://www.douyin.com/live/<数字>`
  - 含上述链接的整段分享文案
  - `v.douyin.com` / `v.iesdouyin.com` 短链接：给出可执行提示（浏览器打开后复制长链接），不再笼统报错
  - 其他无法识别直播间 ID 的链接：提示应使用 `live.douyin.com/<数字>` 形式
- `src/dlrecorder/presentation/main_window.py`：添加对话框提示改为“直播间链接或 ID”，
  先做输入校验并弹出具体原因；重复房间的提示带上 `web_rid`。
- `src/dlrecorder/presentation/widgets.py`：编辑对话框标签改为“直播间 ID 或链接”。
- 未改变数据库 Schema、数据目录规则、解析器与录制流程。

### 2.4 行为兼容

- 纯数字 / 字母 ID 输入行为不变。
- 非法字符、空值、超过 64 字符仍被拒绝（原有 3 项回归保持通过）。
- 已存在房间的编辑同样支持链接输入。

## 3. 测试结果

- 最终完整回归：**209 passed / 0 failed**（离线，29.39s）。
- 0.1.0 的 205 项全部保留并通过；新增 4 项：
  1. `test_validate_web_rid_accepts_live_urls`：完整链接、带参数链接、`www.douyin.com/root/live`、分享文案提取；
  2. `test_validate_web_rid_short_link_gives_actionable_hint`：短链接与未知链接的可执行提示；
  3. `test_add_room_accepts_full_live_url`：按链接添加房间并落库为纯 `web_rid`；
  4. `test_update_room_accepts_full_live_url`：按链接修改房间。
- 未重复 24 小时稳定性采样与真实抖音录制；0.1.0 的通过记录仍然有效，本报告不计为重新验证。

## 4. 构建与产物

```powershell
.venv\Scripts\python.exe -m pytest -q
powershell -NoProfile -ExecutionPolicy Bypass -File build\build.ps1
```

- 程序：`dist\DouyinLiveRecorder\DouyinLiveRecorder.exe`（onedir，无控制台窗口）。
- 所有源码修改时间均早于构建时间；窗口标题与启动日志均显示 `0.1.1`。
- 发布包目录结构：

```
DouyinLiveRecorder-0.1.1-win64\
  DouyinLiveRecorder.exe
  _internal\                     # Qt、Python 运行时、内置 FFmpeg
  README.md
  THIRD_PARTY_NOTICES.txt
  licenses\FFmpeg-LICENSE.txt
  licenses\FFmpeg-README.txt
```

- 发布包**不包含**：`.venv`、测试代码、验收脚本、数据库、录制文件、日志及开发产物。

## 5. 发布验收结果

| 验收项 | 方法 | 结果 |
| --- | --- | --- |
| 自动化回归 | 完整 pytest | **209 passed** |
| 版本一致性 | 窗口标题 / 启动日志 / `__init__.py` / `pyproject.toml` | 一致（0.1.1） |
| 正式构建可启动 | `acceptance_low_spec.ps1 -Mode gui` 启动 `dist` 程序 | 退出码 **0** |
| GUI 冒烟 | 同上，40 条采样（约 588s） | 峰值 **97.13MB**、增长 **4.32MB**、CPU 平均 **0.01%**、判定通过 |
| 干净环境启动 | 复制发布目录到 `%TEMP%\dlr-011-clean`，从 `%TEMP%` 作为 cwd 启动 | 退出码 **0** |
| 数据目录定位 | 未设置 `DLR_DATA_DIR` | 数据创建在 EXE 同级 `data\`，cwd 无 `data` |
| 首启初始化 | 空目录启动 | 自动创建 `data\app.db` 与 `data\logs\app.log` |
| 内置 FFmpeg | 启动日志与文件存在性 | 自动使用 `_internal\ffmpeg\bin\ffmpeg.exe` |
| 发布包内容 | 检查数据库、日志、开发产物 | 均不存在 |
| 用户数据 | 验收期间未接触真实数据 | 全部使用临时目录或 `DLR_DATA_DIR` |

GUI 验收 JSON：`acceptance\acceptance-20261118_011831.json`；启动日志：
`应用初始化，版本=0.1.1`、`解析器: RealDouyinParser`。

说明：以上数据来自本开发机（Core Ultra 5 125H / 18 线程 / 31.47GB），
**不是低配机器验收结论**。

## 6. 数据安全检查

- 发布包不包含 `data\`，首次运行自动初始化空数据库。
- Git 仓库不跟踪 `data\`、录制文件、日志、备份与验收产物。
- 0.1.0 → 0.1.1 升级不迁移、不重建数据库，直接沿用原有 `data\`。
- 测试与验收全部使用临时目录或 `DLR_DATA_DIR`，未修改正式数据库。
- 未发现凭据、Cookie、Token、API Key 被提交或打包。

## 7. 已知限制与未验证项

以下项目与 0.1.0 一致，详见 `docs\FOLLOW_UP_ISSUES.md`：

1. **低配机器（i3 / 4GB）验收未执行**，不得视为通过。
2. **短链接不自动展开**：`v.douyin.com` 需在浏览器打开后复制长链接（本版本仅给出提示）。
3. **未做代码签名**：Windows SmartScreen 可能提示未知发布者。
4. **许可合规**：FFmpeg GPL v3、PySide6/Qt LGPLv3；大规模分发前建议法务复核。
5. **恢复策略残余风险**：当前数据库已损坏且安全副本校验失败时会中止恢复（保持现场）。
6. **自动录制语义**：自动录制开启时，直播中手动停止录制会在下一监控周期再次开始。
7. **解析依赖公开页结构**：抖音页面结构变化时可能需要同步维护解析器。

## 8. 发布检查清单

- [x] 自动化测试全部通过（209 passed）
- [x] 正式构建与源码版本一致（0.1.1）
- [x] EXE 可在干净环境启动，退出码 0
- [x] 内置 FFmpeg 正常加载
- [x] 数据目录定位正确（EXE 同级，`DLR_DATA_DIR` 可覆盖）
- [x] 发布包不含数据库、日志与开发产物
- [x] README、版本号与变更记录一致
- [x] Git 提交与 `v0.1.1` 标签已建立，`v0.1.0` 保持不变
- [x] SHA-256 已重新计算并写入 `release\SHA256SUMS.txt`
- [ ] 低配机器（i3 / 4GB）真机验收（仍待执行）

## 9. 回滚与支持

- 如需回退，使用 `release\DouyinLiveRecorder-0.1.0-win64.zip`（SHA-256 保留在 `SHA256SUMS.txt`）。
- 升级或回退前，建议用程序内「备份数据」生成一致性备份。
- 运行数据保存在程序同级 `data\`；替换程序目录时请保留该目录。
- 问题排查顺序：`data\logs\app.log` → 「设置」中 FFmpeg 检测 → 恢复备份 → 核对版本号。
