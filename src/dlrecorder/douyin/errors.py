"""抖音解析错误类型。

大部分错误通过 ParseResult 枚举表达，此处仅定义需要异常传播的场景。
"""
from __future__ import annotations


class DouyinParserError(Exception):
    """抖音解析相关错误的基类。"""


class RoomNotExistError(DouyinParserError):
    """直播间不存在。"""
