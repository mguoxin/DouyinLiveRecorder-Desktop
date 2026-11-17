"""pytest 全局配置：统一 Qt 应用对象的创建与复用。

Qt 只允许存在一个 QCoreApplication/QApplication 单例，且不允许先创建
QCoreApplication 再创建 QApplication。因此整个测试会话统一由本文件提供
唯一的 QApplication，测试模块不得自行创建 Qt 应用对象。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 测试默认使用 Mock 解析器，禁止真实网络请求；生产默认见 app._build_parser。
os.environ.setdefault("DLR_PARSER", "mock")

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qt_app():
    """会话级唯一的 QApplication。"""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    assert isinstance(app, QApplication), "测试必须复用同一个 QApplication"
    return app
