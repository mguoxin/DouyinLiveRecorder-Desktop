# 0.3.1 直播间链接解析修复

## 1. 问题

v0.3.0 用户验收时，添加下列链接提示
「无法从链接中识别直播间 ID，请使用 live.douyin.com/<数字> 形式的直播间地址」：

```
https://www.douyin.com/follow/live/805329551023?anchor_id=4352283890695387
```

## 2. 原因

旧实现用一条正则匹配固定前缀：

```
live.douyin.com/<rid>
(www.)douyin.com/(root/)?live/<rid>
```

`/follow/live/<rid>` 不在支持范围内，匹配失败后只能落到「含 `://` 即报错」的兜底分支；
同时该错误提示与实际支持能力不符，用户无法从提示中得知哪种链接可用。

## 3. 修复方案

改为**按链接结构解析**（`urllib.parse.urlsplit`），不再依赖固定前缀正则：

| 链接形态 | 取房间号的位置 |
| --- | --- |
| `live.douyin.com/<数字>` | 路径首段 |
| `www.douyin.com/live/<数字>` | `live` 段之后的整段数字 |
| `www.douyin.com/root/live/<数字>` | 同上 |
| `www.douyin.com/follow/live/<数字>` | 同上（本次问题链接） |
| 任意受支持域名 + `?live_web_rid=<数字>` | 查询参数兜底（只在路径取不到时使用） |
| 含分享文案的整段文本 | 先定位抖音链接再按上述规则解析 |

约束（对应任务的安全要求）：

- 房间号只认**路径中的整段数字**（`^\d{1,20}$`）或 `live_web_rid` 参数；
  `anchor_id`、`user_id` 等参数不参与识别，不会从 URL 任意位置抓数字。
- 域名白名单：`live.douyin.com`、`www.douyin.com`、`douyin.com`；
  仅包含 `douyin.com` 子串的相似域名（如 `fakedouyin.com`）不会被接受。
- 错误提示区分三种情况：短链接（`v.douyin.com`，提示到浏览器打开）、
  不支持的域名（提示仅支持抖音域名）、抖音链接但路径无房间号（提示可用格式）。
- 纯数字 ID 直接添加、带尾部斜杠、多个查询参数、分享文案等原有行为保持不变。

## 4. 修改范围

| 文件 | 变更 |
| --- | --- |
| `src/dlrecorder/domain/models.py` | 新增 `_rid_from_douyin_url`（结构化解析）与域名白名单；重写 `_extract_web_rid` 的识别与错误提示；移除固定前缀正则 |
| `src/dlrecorder/presentation/main_window.py` | 「添加直播间」输入提示补充 `follow/live` 示例 |
| `tests/test_room_url.py` | 新增 14 项回归（含用户上报链接作为固定样本） |
| `README.md`、`THIRD_PARTY_NOTICES.txt`、`pyproject.toml`、`src/dlrecorder/__init__.py` | 版本号 0.3.1 与变更记录 |

未修改解析引擎、监控、录制、数据库与设置逻辑。

## 5. 自动化测试

```
.venv\Scripts\python.exe -m pytest tests\test_room_url.py tests\test_app.py -q
→ 47 passed
```

新增用例覆盖：标准 `live.douyin.com` 链接、本次 `follow/live` 链接、`anchor_id` 链接、
多参数 + 尾部斜杠、仅有 `live_web_rid` 的首页链接、分享文案、`root/live` 与 `live` 旧路径、
路径无房间号、不支持的域名（含相似域名 `fakedouyin.com`）、短链接提示、纯数字 ID、非法 ID、
用户主页链接不被误判。

全量回归见第 7 节。

## 6. 真实链接验证（生产解析器，非 Mock）

```
LINK = https://www.douyin.com/follow/live/805329551023?anchor_id=4352283890695387
PARSED_WEB_RID = 805329551023
STATUS = 2          （2 = 直播中）
RESULT = SUCCESS
STREAM_URL = http://pull-flv-l13.douyincdn.com/third/stream-408691124008649491_uhd...
```

即：解析出的房间号正确（不是 `anchor_id` 的 `4352283890695387`），且该房间当时确实在直播、
可取到流地址，说明修复后的房间号可直接用于监控与录制。

## 7. 打包版验证（release 目录，非源码）

在隔离数据目录（`DLR_DATA_DIR` 指向 `%TEMP%`）启动
`release\DouyinLiveRecorder-0.3.1-win64\DouyinLiveRecorder.exe`：

- 窗口标题与启动日志均为 `v0.3.1`，日志 `解析器: EngineDouyinParser`；
- 通过界面「添加房间」粘贴用户上报链接，落库房间号为 `805329551023`
  （不是 `anchor_id` 的 `4352283890695387`）；
- 开始监控后界面显示 `LIVE / SUCCESS`；
- 停止监控、关闭窗口后进程退出码 0，无残留 `DouyinLiveRecorder.exe` 与 `ffmpeg.exe`。

## 8. 回归与发布

- 全量回归：**273 passed**（0.3.0 的 259 项 + 本次新增 14 项）。
- 版本：`0.3.1`（补丁版本）。原因：0.3.0 发布包对用户上报链接存在可见的功能缺失，
  必须重新构建发布包才能真正修复；不改动 `v0.3.0` 标签与既有发布物。
- 发布物：`release\DouyinLiveRecorder-0.3.1-win64.zip`（81,499,978 字节），
  对应条目已追加到 `release\SHA256SUMS.txt`：
  `FBE2CC654B24CC40310A6C8813692DFA2D1490AAD6B4A5BB2A2F57A194A96E3E`。
- 发布目录 174 个文件、201.97 MB，不含 `data\`、Cookie、数据库与录制文件。
