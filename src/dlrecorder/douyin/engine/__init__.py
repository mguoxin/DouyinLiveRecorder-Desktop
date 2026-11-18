"""抖音直播解析引擎（移植自 ihmily/DouyinLiveRecorder v4.0.7，MIT）。

本包只保留抖音链路所需的实现，并按本项目的解析器接口做适配：

- ``ab_sign``：上游的 ``a_bogus`` 签名算法（纯 Python，无外部运行时）。
- ``douyin``：``webcast/room/web/enter`` 接口获取房间数据与流地址选择。
- ``parser``：``EngineDouyinParser``，实现本项目 ``DouyinParser`` 接口。

上游版权与许可证见仓库根目录 ``THIRD_PARTY_NOTICES.txt``。
"""
