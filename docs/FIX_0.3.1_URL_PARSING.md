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

- 房间号只认**路径中的整段 ASCII 数字**（`^[0-9]{1,20}$`）或 `live_web_rid` 参数；
  `anchor_id`、`user_id` 等参数不参与识别，不会从 URL 任意位置抓数字；
  全角数字、阿拉伯-印度数字等 Unicode 数字不会被当作房间号。
- 域名白名单：`live.douyin.com`、`www.douyin.com`、`douyin.com`；
  仅包含 `douyin.com` 子串的相似域名（如 `fakedouyin.com`）不会被接受。
- 支持显式端口（如 `https://live.douyin.com:443/<数字>`）；域名大小写、尾部斜杠、
  `#fragment`、多个查询参数均可正常处理。
- 错误提示区分三种情况：短链接（`v.douyin.com`，提示到浏览器打开）、
  不支持的域名（提示仅支持抖音域名）、抖音链接但路径无房间号（提示可用格式）。
- 纯数字 ID 直接添加、带尾部斜杠、多个查询参数、分享文案等原有行为保持不变。

## 3.1 独立验收中发现并修复的两个边界缺陷

v0.3.1 自验收阶段对解析器做了额外边界探测，发现并修复：

1. **Unicode 数字被当作房间号**：原实现用 `\d{1,20}` 校验路径段，而 Python 的 `\d`
   会匹配全角数字（`８０５`）与阿拉伯-印度数字（`١٢٣` 等）。这类输入会被当作房间号
   传给上游解析器。已改为 ASCII 范围 `[0-9]{1,20}`，此类链接现在按“路径中没有有效的
   直播间号”拒绝。
2. **显式端口无法识别**：原链接定位正则要求主机名后紧跟 `/`，
   `https://live.douyin.com:443/<数字>` 无法匹配，且错误提示把 `:443` 当作域名的一部分
   （显示“不支持的域名 live.douyin.com:443”）。已允许可选端口，错误提示只显示主机名。

两项修复均补充了自动化回归测试。

## 4. 修改范围

| 文件 | 变更 |
| --- | --- |
| `src/dlrecorder/domain/models.py` | 新增 `_rid_from_douyin_url`（结构化解析）与域名白名单；重写 `_extract_web_rid` 的识别与错误提示；移除固定前缀正则 |
| `src/dlrecorder/presentation/main_window.py` | 「添加直播间」输入提示补充 `follow/live` 示例 |
| `tests/test_room_url.py` | 新增 17 项回归（含用户上报链接作为固定样本） |
| `README.md`、`THIRD_PARTY_NOTICES.txt`、`pyproject.toml`、`src/dlrecorder/__init__.py` | 版本号 0.3.1 与变更记录 |

未修改解析引擎、监控、录制、数据库与设置逻辑。

## 5. 自动化测试

```
.venv\Scripts\python.exe -m pytest tests\test_room_url.py tests\test_app.py -q
→ 50 passed
```

新增用例覆盖：标准 `live.douyin.com` 链接、本次 `follow/live` 链接、`anchor_id` 链接、
多参数 + 尾部斜杠、仅有 `live_web_rid` 的首页链接、分享文案、`root/live` 与 `live` 旧路径、
路径无房间号、不支持的域名（含相似域名 `fakedouyin.com` 与带端口的非抖音域名）、短链接提示、
纯数字 ID、非法 ID、显式端口与域名大小写、全角/阿拉伯数字不被当作房间号、用户主页链接不被误判。

全量回归见第 8 节。

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

- 全量回归：**276 passed**（0.3.0 的 259 项 + 本次新增 17 项）。
- 版本：`0.3.1`（补丁版本）。原因：0.3.0 发布包对用户上报链接存在可见的功能缺失，
  必须重新构建发布包才能真正修复；不改动 `v0.3.0` 标签与既有发布物。
- 发布物：`release\DouyinLiveRecorder-0.3.1-win64.zip`（81,501,283 字节），
  对应条目已追加到 `release\SHA256SUMS.txt`：
  `50053355A70D6FDED5A1A3F8E408AAEF5DADFD00461303664A7EEF0599434395`。
- 发布目录 174 个文件、201.97 MB，不含 `data\`、Cookie、数据库与录制文件；
  解压后与发布目录逐文件 SHA-256 比对一致。

## 9. 独立验收（2026-11-18）

- URL 解析边界探测发现并修复 2 个缺陷（见 3.1），新增 3 项回归。
- 测试：URL 专项 + 应用回归 50 passed；全量 **276 passed**。
- 打包版冒烟：窗口标题 `v0.3.1`，添加用户上报链接 → 房间号 `805329551023` → `LIVE / SUCCESS`，
  停止监控后关闭，退出码 0，无残留 `DouyinLiveRecorder.exe` / `ffmpeg.exe`。
- 发布包：ZIP 与目录 174 个文件逐文件哈希一致；无数据库、Cookie、录制文件；
  SHA-256 与 `release\SHA256SUMS.txt` 一致。
- Git：`v0.3.0` 标签保持 `0a6d33c` 不变；`v0.3.1` 指向本次最终交付提交。
- 发布目录 174 个文件、201.97 MB，不含 `data\`、Cookie、数据库与录制文件。
