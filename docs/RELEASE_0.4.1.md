# v0.4.1 发布说明

发布日期：2026-11-22
适用平台：Windows 10 / 11 x64（便携版，解压即用）

正式包：`DouyinLiveRecorder-0.4.1-win64.zip`（109,797,739 字节）
SHA-256：`2C21A212515357587E510B43F3DE55531A3899391A417DA54B746A7812617C95`
EXE SHA-256：`3DC1B057A4E8589BFB2528FF3EA16E7069CB37AE8A115BAEE363DA9B53E095A1`

## 更新内容

### 1. 会话与分段结束事件修复（P2-01）

- 会话级结束事件与分段级结束事件分离：会话结束事件携带 `scope=session`，
  不再复用上一个分段的 `file_path` 与 `file_size`，也不再出现“状态栏显示
  `completed size=0` 但历史表有实际大小”的不一致。
- 分段结束事件保持 `scope=segment`，状态栏、日志与数据库历史语义一致。
- 新增/覆盖用例：直播结束收尾、续录耗尽、等待续录期间手动停止、会话结束失败清理。

### 2. FFmpeg 诊断与性能

- 持续排空 FFmpeg `stderr`，避免管道写满导致录制卡死；实测 3.8 MB / 30,000 行在
  0.46 秒内排空，正常录制开销可忽略。
- 诊断日志对 URL 中的令牌、Cookie 等敏感参数做脱敏，不写入明文凭据。
- 录制停止时按“先请求退出、再限时等待、最后终止”的顺序清理，不遗留读取线程。

### 3. 关闭流程与稳定性

- 修复“主窗口隐藏（托盘）且仍在录制时，退出确认框不可见导致关闭长时间阻塞”的问题：
  弹框前先恢复并激活主窗口。
- 关闭期间 `is_running()` 的超时异常不再冒泡到 Qt 槽。
- 桌面复测：正常关闭 0.74 秒、连续 5 次重复关闭、隐藏窗口场景确认框可见、无残留进程。

### 4. 许可证与 FFmpeg 替换

- 内置 FFmpeg 改为 **BtbN `win64-lgpl-shared`**（LGPL-3.0-or-later，未修改、动态链接，
  DLL 可整体替换），版本 `n9.0.2-61-gd59078794d`，构建提交 `7f2a6b1`。
- 随包提供：`LICENSES\`（LGPL-2.0/2.1/3.0、MIT、BSD、ISC、Apache-2.0、MPL-2.0 等全文）、
  `FFMPEG-COMPONENT-NOTICES.txt`（70 个组件的版本/许可/版权/来源）、
  `ffmpeg-components\`（94 份上游许可原文）、`SOURCE-MANIFEST.csv`、`FFMPEG-SOURCE-OFFER.txt`。
- Release 同时提供 19 份静态链接 LGPL 组件的对应源码归档与校验清单。

### 5. 工程质量

- GitHub Actions 增加 Windows + Python 3.12 的真实测试流水线，连续多轮通过。
- 单实例测试在非中文代码页环境下改用 UTF-8 子进程编码，修复 Windows Runner 失败。
- README、审计文档与发布流程补充构建、校验与合规说明。

## 测试结果

- 本地：**284 项通过**（含 3 项依赖真实 FFmpeg 二进制的用例）。
- GitHub Actions（`windows-latest`）：**281 passed / 0 failed / 3 skipped**
  （跳过项为 CI 环境未内置 FFmpeg），连续 5 轮以上成功。

## 安装与升级

- 解压到任意可写目录，双击 `DouyinLiveRecorder.exe`；无需安装 Python 或 FFmpeg，不写注册表。
- 升级：退出旧版后覆盖解压即可；配置与录制历史位于 `data\`，**旧数据可直接沿用**，
  数据库 Schema 仍为 v2，无迁移步骤。
- 首次使用：侧边栏「直播间」→「添加直播间」→ 粘贴链接或数字 ID → 开始监控/录制。

## 下载与校验

- 发布页：<https://github.com/mguoxin/DouyinLiveRecorder-Desktop/releases/tag/v0.4.1>
- 附件：正式包、`v0.4.1-SHA256SUMS.txt`、19 份源码归档、源码 `SHA256SUMS.txt` 与 `MANIFEST.csv`。
- Windows 校验：

```powershell
Get-FileHash .\DouyinLiveRecorder-0.4.1-win64.zip -Algorithm SHA256
# 应为 2C21A212515357587E510B43F3DE55531A3899391A417DA54B746A7812617C95
```

## 已知限制（如实披露）

1. **未重新验证真实抖音直播录制链路**：解析、拉流、断线续录、弱网行为未在本版真实直播间复测。
2. **“异常后窗口空白”未复现、未消除**：保留观察项与复现步骤，不作已修复声明。
3. **FFmpeg kill 回退路径未完成专项实测**：Windows 下 terminate 近似强杀，kill 分支难以触发。
4. **低配 Windows 设备未单独验收**：仅有 2 核亲和性模拟基准。
5. **构建工具链未达到位级可复现**：未固定完整工具链，亦未实操从源码重建 FFmpeg DLL。
6. **LCEVC 第三方专利风险**：`lcevcdec` 许可为 BSD-3-Clause-Clear，不授予专利许可，
   使用者需结合自身用途评估。

本版本不宣称所有潜在问题均已解决。

## 许可证

- 本项目原创代码：MIT（见 `LICENSE`）。
- 内置 FFmpeg 与第三方组件：LGPL / MIT / BSD / ISC / Apache-2.0 / MPL-2.0 / Zlib / WTFPL 等，
  全文与版权声明见 `THIRD_PARTY_NOTICES.txt`、`LICENSES\`。
- v0.4.0 的 GPLv3 静态构建许可问题与本版本无关；v0.4.0 Release 保持 Draft，不提供公开下载。
