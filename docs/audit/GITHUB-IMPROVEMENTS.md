# GITHUB-IMPROVEMENTS — 仓库对外质量与工程流程

> 基线：`mguoxin/DouyinLiveRecorder-Desktop`，提交 `c8ec7c2`，公开仓库，v0.4.0 已发布。
> 本文只给建议与最小动作，**未修改 README、文档或仓库配置**。
> 原则：不重复建设已存在且有效的资产（见第 5 节）。

---

## 1. 事实基线

| 项目 | 现状 | 证据 |
| --- | --- | --- |
| README | 196 行，0 张图片引用 | `Select-String '!\[' README.md` 命中 0 |
| 下载入口 | 只写本地相对路径，且为 **0.1.1** | `README.md:161`：「正式发布包：`release\DouyinLiveRecorder-0.1.1-win64.zip`（约 75MB）」 |
| GitHub 链接 | 全文无 `github.com` 字样 | 同上检索 |
| 文档 | `docs/` 下 20 个 md，无索引文件 | 目录列举 |
| 截图 | 3 个目录共 52 张（`gui-acceptance-20261121` 10、`gui-candidate-20261121` 31、`release-0.4.0` 11） | 目录统计 |
| 许可证 | 无 `LICENSE` | `git ls-files` 根目录仅 5 个文件 |
| 贡献规则 | 无 `CONTRIBUTING.md`、无 Issue/PR 模板、无 `SECURITY.md` | `git ls-files` 匹配数 0 |
| CI | 无 `.github/` 目录 | `git ls-files` 匹配数 0 |
| 构建 | `build/build.ps1`、`build/pyinstaller.spec`、`build/make_icon.py`、`build/verify_icon.py` | 目录列举 |
| 发布校验 | `release/SHA256SUMS.txt` 已存在且覆盖 0.4.0 | 文件内容 |

---

## 2. 新用户路径问题（G-01 / G-02 / G-08）

**结论：目前不能在新用户 30 秒内完成「找到下载 → 安装 → 首次使用」。**

原因链：

1. 仓库 README 首屏（L3-11）先讲版本沿革和本地相对路径，没有可点击的 Release 链接。
2. L161 的发布包路径指向 **0.1.1**，且该 zip 不在仓库中（`release/` 目录未入库），点击即 404 或无处可寻。
3. README 没有任何界面截图，新用户无法判断这是 GUI 应用还是命令行工具。
4. 系统要求、安装步骤、首次添加直播间的操作路径散落在 L57 之后的章节，没有「快速开始」。

### G-01 重写 README 首屏（P0，纯文档）

最小内容骨架（建议 25 行内）：

```markdown
# 抖音直播录制工具（Windows 桌面版）

[![Release](https://img.shields.io/badge/release-v0.4.0-blue)](https://github.com/mguoxin/DouyinLiveRecorder-Desktop/releases/latest)
[![Platform](https://img.shields.io/badge/platform-Windows%20x64-lightgrey)]()
[![Python](https://img.shields.io/badge/python-3.12-blue)]()

Windows 桌面端抖音直播监控与录制工具：GUI 管理直播间、多房间并发监控、
FFmpeg 录制、自动续录、内嵌 FFmpeg 解压即用。

## 下载
- 最新正式版：[v0.4.0 Release](https://github.com/mguoxin/DouyinLiveRecorder-Desktop/releases/latest)
- 校验：下载 `SHA256SUMS.txt` 后执行 `certutil -hashfile <zip> SHA256`

## 快速开始（3 步）
1. 解压 `DouyinLiveRecorder-0.4.0-win64.zip` 到任意目录
2. 双击 `DouyinLiveRecorder.exe`
3. 侧边栏「直播间」→「添加直播间」→ 粘贴链接 → 开始录制

## 截图
（引用 docs/screenshots/release-0.4.0 下 2-3 张）

## 系统要求
Windows 10/11 x64；无需安装 Python 或 FFmpeg。
```

### G-02 清理过期版本叙述（P1，纯文档）

- 修正 `README.md:161` 为 0.4.0，或直接删除「正式发布包：…」段落，改为指向 Releases。
- L118、L226 等历史版本说明保留，但建议加一行「以下为历史版本记录，最新版见 Releases」。

### G-08 README 引用真实截图（P1，纯文档）

- 从 `docs/screenshots/release-0.4.0/` 选 2–3 张（主界面浅色、深色设置页、托盘菜单）插入 README。
- 现有截图已做隐私裁剪（见 `docs/RELEASE_0.4.0.md`），可直接复用，无需重新截图。

---

## 3. 许可证与贡献规则（G-03 / G-07）

### G-03 许可证决策（P1，需你拍板）

事实缺口（不是法律结论）：

- 仓库无 `LICENSE`，GitHub 不会展示授权信息，他人默认「保留所有权利」。
- 依赖含 PySide6（LGPL-3.0 或商业授权二选一）与 FFmpeg（取决于构建配置，GPL 或 LGPL）。`THIRD_PARTY_NOTICES.txt` 已存在，但仅覆盖第三方声明，不构成项目自身授权。

建议动作（二选一，由你决定）：

- **A**：加 `LICENSE`（如 MIT/Apache-2.0）并在 README 加「许可」小节，同时保留 `THIRD_PARTY_NOTICES.txt`；注意 FFmpeg 若为 GPL 构建，整体分发义务会变严。
- **B**：明确声明「源码公开但未授权再分发」，README 写清限制，避免误解。

无论哪种，都建议在 README 末尾加一行指向 `THIRD_PARTY_NOTICES.txt`。

### G-07 贡献与安全（P2）

- `.github/ISSUE_TEMPLATE/bug_report.md`（要求附版本号、日志片段、复现步骤，并提醒**先脱敏绝对路径**）。
- `.github/PULL_REQUEST_TEMPLATE.md`（要求说明是否改 Schema/事件契约、是否跑全量测试）。
- `SECURITY.md`（说明这是本地录制工具、不上传用户数据、漏洞私下报告渠道）。

---

## 4. CI 与发布流程（G-05 / G-06）

### G-05 最小可行 CI（P0，纯新增文件）

可行性依据：

- `tests/conftest.py:7-24` 已设置 `QT_QPA_PLATFORM=offscreen` 与 `DLR_PARSER=mock`，GUI 可在无显示环境运行。
- `tests/test_real_ffmpeg.py:18-22` 在找不到 FFmpeg 时 `pytest.skip`，CI 上会自然跳过，不会失败。
- 依赖仅 `PySide6-Essentials>=6.6`、`httpx[http2]`，`windows-latest` 可直接安装。

建议结构（单 job，约 30 行）：

```yaml
name: CI
on: [push, pull_request]
jobs:
  test:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.12' }
      - run: pip install -e ".[test]"
      - run: pytest -q
```

**预期结果**：0 failed；真实 FFmpeg 用例 skip（数量会少于本地 276 passed，这是正常差异，需在 CI 日志里如实记录 skipped 数）。不要把「CI 通过」等同于「276 项全通过」。

后续可选（P2）：`windows-latest` 上加一步下载 FFmpeg 并运行 `tests/test_real_ffmpeg.py`，作为 nightly 而非每 PR。

### G-06 发布流程脚本化（P2）

已有且可复用：`build/build.ps1` 负责构建，`build/verify_icon.py` 负责图标一致性，`release/SHA256SUMS.txt` 已手工维护。

缺口：从构建到「生成校验和 → 上传 Release 附件 → 回读校验」仍需人工串联。建议新增一个**本地** `build/release.ps1`：

1. 校验 `pyproject.toml` / `version_info.txt` / 包内 `__version__` 三处版本一致；
2. 构建 → 生成 zip；
3. 计算 zip SHA-256 并追加/更新 `SHA256SUMS.txt`；
4. 从 zip 内解出 EXE 校验哈希与版本资源；
5. 打印待上传清单（不自动 push、不自动建 Release，避免误操作）。

不建议把发布放进 CI：需要仓库写权限令牌，风险大于收益，且当前是单人维护。

---

## 5. 不要重复建设（明确排除）

| 已有资产 | 不要再做 |
| --- | --- |
| `build/build.ps1` + `pyinstaller.spec` | 另起一套构建脚本或引入 Nuitka/Briefcase |
| `build/verify_icon.py` | 重做图标校验流程 |
| `release/SHA256SUMS.txt` + Release 附件 | 重建第二份校验清单 |
| `docs/RELEASE_0.4.0.md`（含图标/版本资源/冒烟记录） | 重写发布说明 |
| 52 张验收截图 | 重新截图或引入截图工具 |
| `docs/AUDIT_0.3.1_RECORDING_STATUS.md` | 重新做一遍录制状态审计 |

---

## 6. 文档结构（G-04 / G-09）

### G-04 文档索引（P1，纯文档）

`docs/` 现有 20 篇，最早可追溯到 0.1.0。建议新增 `docs/README.md` 作为索引，分三栏：

| 分类 | 文档 |
| --- | --- |
| 现行（对外） | `RELEASE_0.4.0.md`、`PROJECT_STATUS.md`、`FOLLOW_UP_ISSUES.md`、`audit/*`（本次新增） |
| 技术参考 | `CAPABILITY_BASELINE.md`、`CAPABILITY_MATRIX.md`、`MIGRATION_UPSTREAM_0.2.0.md` |
| 历史归档（只读） | `RELEASE_REPORT_0.1.*`、`DELIVERY_0.2.0/0.3.0`、`ACCEPTANCE_*`、`GUI_DELIVERY_*`、`FINAL_ACCEPTANCE_*`、`PHASE2_REPORT`、`P0_STABILITY_REPORT`、`TEST_REPORT_0.3.0`、`UPSTREAM_AUDIT_0.1.1`、`FIX_0.3.1_URL_PARSING` |

同时把仓库根目录的 `阶段3A报告.md` 移入 `docs/` 归档区（根目录文件名与仓库英文命名风格不一致，且会干扰新用户）。

### G-09 仓库元数据（P2）

- About 区补充 Topics：`douyin`、`live-recording`、`pyside6`、`ffmpeg`、`windows`。
- Release 说明已完整（含 P2-01 登记），保持现状，不重复改写。

---

## 7. 优先级汇总

| ID | 动作 | 优先级 | 类型 | 预估改动 |
| --- | --- | --- | --- | --- |
| G-01 | README 首屏重写 | P0 | 文档 | 1 文件 |
| G-05 | 最小 CI | P0 | 新增 1 文件 | 1 文件 |
| G-02 | 修正 0.1.1 残留 | P1 | 文档 | 1 处 |
| G-08 | README 插图 | P1 | 文档 | 1 处 |
| G-03 | 许可证决策 | P1 | 决策 + 1 文件 | 1 文件 |
| G-04 | 文档索引与归档 | P1 | 文档 | 1 新增 + 1 移动 |
| G-06 | 发布脚本化 | P2 | 新增脚本 | 1 文件 |
| G-07 | 模板与 SECURITY | P2 | 新增 3-4 文件 | 低 |
| G-09 | Topics | P2 | 仓库设置 | 0 代码 |
