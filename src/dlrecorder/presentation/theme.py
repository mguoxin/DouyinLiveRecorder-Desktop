"""外观主题：设计令牌、QSS/QPalette 应用与系统主题跟随。

只影响 GUI 呈现，不触碰录制、解析、监控与数据库业务逻辑。
主题偏好保存在既有 settings 表的 ``appearance.theme`` 键中。
"""
from __future__ import annotations

import ctypes
import logging
import sys
from dataclasses import dataclass
from string import Template
from typing import Optional

from PySide6.QtCore import QLibraryInfo, QObject, Qt, QTranslator, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication, QStyleFactory, QWidget

from ..infrastructure.db import Database

logger = logging.getLogger(__name__)

THEME_SETTING_KEY = "appearance.theme"
MODE_SYSTEM = "system"
MODE_LIGHT = "light"
MODE_DARK = "dark"
THEME_MODES = (MODE_SYSTEM, MODE_LIGHT, MODE_DARK)
MODE_LABELS = {
    MODE_SYSTEM: "跟随系统",
    MODE_LIGHT: "浅色",
    MODE_DARK: "深色",
}

UI_FONT_FAMILY = "Microsoft YaHei UI"
UI_FONT_SIZE_PT = 9.0
MONO_FONT_FAMILY = "Cascadia Mono"


@dataclass(frozen=True)
class ThemeTokens:
    """一套主题的语义颜色。所有控件只引用语义名，不直接写死颜色。"""

    bg: str
    bg_alt: str
    surface: str
    surface_alt: str
    elevated: str
    border: str
    border_strong: str
    text: str
    text_secondary: str
    text_muted: str
    accent: str
    accent_hover: str
    accent_pressed: str
    accent_fg: str
    accent_soft: str
    selection: str
    hover: str
    success: str
    success_soft: str
    warning: str
    warning_soft: str
    danger: str
    danger_soft: str
    info: str
    info_soft: str


LIGHT = ThemeTokens(
    bg="#F5F6F8",
    bg_alt="#EEF0F3",
    surface="#FFFFFF",
    surface_alt="#FAFBFC",
    elevated="#FFFFFF",
    border="#E3E6EA",
    border_strong="#D3D8DE",
    text="#1B1F24",
    text_secondary="#5B6573",
    text_muted="#8A94A3",
    accent="#E11D48",
    accent_hover="#C9183E",
    accent_pressed="#AD1435",
    accent_fg="#C9183E",
    accent_soft="#FCE7EC",
    selection="#E9EDF2",
    hover="#F0F2F5",
    success="#17803D",
    success_soft="#E4F5EA",
    warning="#B45309",
    warning_soft="#FDF1DF",
    danger="#C62828",
    danger_soft="#FCE8E8",
    info="#1D4ED8",
    info_soft="#E7EEFD",
)

DARK = ThemeTokens(
    bg="#15171A",
    bg_alt="#1A1D21",
    surface="#1E2126",
    surface_alt="#23272C",
    elevated="#2A2E34",
    border="#30353C",
    border_strong="#3C424A",
    text="#E8EAED",
    text_secondary="#A8B1BC",
    text_muted="#7D8792",
    accent="#E11D48",
    accent_hover="#F43F5E",
    accent_pressed="#C9183E",
    accent_fg="#FF708A",
    accent_soft="#3A1B24",
    selection="#2B3138",
    hover="#262B31",
    success="#3FB950",
    success_soft="#1C3326",
    warning="#D29922",
    warning_soft="#332B1C",
    danger="#F85149",
    danger_soft="#3A2222",
    info="#58A6FF",
    info_soft="#1C2A3A",
)

_QSS = Template(
    """
QWidget {
    color: $text;
}
QMainWindow, QDialog {
    background: $bg;
}
QWidget#Sidebar {
    background: $surface;
    border-right: 1px solid $border;
}
QWidget#ContentRoot {
    background: $bg;
}
QWidget#Page {
    background: $bg;
}
QLabel#PageTitle {
    font-size: 19px;
    font-weight: 600;
    color: $text;
}
QLabel#PageSubtitle {
    font-size: 12px;
    color: $text_secondary;
}
QLabel#BrandTitle {
    font-size: 13px;
    font-weight: 600;
    color: $text;
}
QLabel#BrandSubtitle {
    font-size: 10px;
    color: $text_muted;
}
QLabel#SectionTitle {
    font-size: 13px;
    font-weight: 600;
    color: $text;
}
QLabel#Muted {
    color: $text_muted;
}
QLabel#Secondary {
    color: $text_secondary;
}
QLabel#ErrorLabel {
    color: $danger;
}
QLabel#SuccessLabel {
    color: $success;
}
QLabel#EmptyTitle {
    font-size: 15px;
    font-weight: 600;
    color: $text;
}
QLabel#EmptyText {
    font-size: 12px;
    color: $text_muted;
}
QFrame#Card {
    background: $surface;
    border: 1px solid $border;
    border-radius: 10px;
}
QFrame#Divider {
    background: $border;
    border: none;
    max-height: 1px;
    min-height: 1px;
}
QFrame#VDivider {
    background: $border;
    border: none;
    max-width: 1px;
    min-width: 1px;
}
QPushButton {
    background: $surface;
    border: 1px solid $border_strong;
    border-radius: 6px;
    padding: 6px 13px;
    min-height: 20px;
    color: $text;
}
QPushButton:hover {
    background: $hover;
    border-color: $border_strong;
}
QPushButton:pressed {
    background: $selection;
}
QPushButton:disabled {
    color: $text_muted;
    background: $surface_alt;
    border-color: $border;
}
QPushButton:focus {
    outline: none;
}
QPushButton[variant="primary"] {
    background: $accent;
    border-color: $accent;
    color: #FFFFFF;
    font-weight: 600;
}
QPushButton[variant="primary"]:hover {
    background: $accent_hover;
    border-color: $accent_hover;
}
QPushButton[variant="primary"]:pressed {
    background: $accent_pressed;
    border-color: $accent_pressed;
}
QPushButton[variant="primary"]:disabled {
    background: $border_strong;
    border-color: $border_strong;
    color: $surface;
}
QPushButton[variant="danger"] {
    color: $danger;
    border-color: $border;
    background: transparent;
}
QPushButton[variant="danger"]:hover {
    background: $danger_soft;
    border-color: $danger;
}
QPushButton[variant="ghost"] {
    background: transparent;
    border-color: transparent;
    color: $text_secondary;
}
QPushButton[variant="ghost"]:hover {
    background: $hover;
    color: $text;
}
QPushButton[variant="ghost"]:pressed {
    background: $selection;
}
QPushButton[nav="true"] {
    text-align: left;
    padding: 8px 12px;
    border: none;
    border-radius: 8px;
    background: transparent;
    color: $text_secondary;
    font-weight: 500;
}
QPushButton[nav="true"]:hover {
    background: $hover;
    color: $text;
}
QPushButton[nav="true"]:checked {
    background: $accent_soft;
    color: $accent_fg;
    font-weight: 600;
}
QPushButton[segment="true"] {
    border-radius: 6px;
    border: 1px solid $border_strong;
    background: $surface;
    padding: 5px 14px;
    color: $text_secondary;
}
QPushButton[segment="true"]:checked {
    background: $accent_soft;
    border-color: $accent;
    color: $accent_fg;
    font-weight: 600;
}
QLineEdit, QSpinBox, QComboBox {
    background: $surface;
    border: 1px solid $border_strong;
    border-radius: 6px;
    padding: 5px 8px;
    min-height: 20px;
    selection-background-color: $accent;
    selection-color: #FFFFFF;
}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {
    border-color: $accent;
}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {
    background: $surface_alt;
    color: $text_muted;
}
QSpinBox::up-button, QSpinBox::down-button {
    width: 0px;
    border: none;
    background: transparent;
}
QLineEdit::placeholder {
    color: $text_muted;
}
QComboBox::drop-down {
    border: none;
    width: 22px;
}
QComboBox QAbstractItemView {
    background: $elevated;
    border: 1px solid $border_strong;
    border-radius: 6px;
    padding: 4px;
    outline: 0;
    selection-background-color: $accent_soft;
    selection-color: $text;
}
QCheckBox {
    color: $text;
    spacing: 7px;
}
QCheckBox::indicator {
    width: 15px;
    height: 15px;
    border: 1px solid $border_strong;
    border-radius: 4px;
    background: $surface;
}
QCheckBox::indicator:hover {
    border-color: $accent;
}
QCheckBox::indicator:checked {
    background: $accent;
    border-color: $accent;
}
QTableWidget {
    background: $surface;
    alternate-background-color: $surface_alt;
    border: none;
    gridline-color: transparent;
    selection-background-color: $selection;
    selection-color: $text;
    outline: 0;
}
QTableWidget::item {
    padding: 6px 10px;
    border-bottom: 1px solid $border;
}
QTableWidget::item:hover {
    background: $hover;
}
QHeaderView::section {
    background: $surface_alt;
    color: $text_secondary;
    border: none;
    border-bottom: 1px solid $border;
    padding: 7px 10px;
    font-weight: 600;
}
QTableCornerButton::section {
    background: $surface_alt;
    border: none;
}
QPlainTextEdit {
    background: $surface;
    border: 1px solid $border;
    border-radius: 8px;
    padding: 8px;
    selection-background-color: $accent;
    selection-color: #FFFFFF;
}
QScrollArea {
    border: none;
    background: transparent;
}
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background: $border_strong;
    border-radius: 4px;
    min-height: 28px;
}
QScrollBar::handle:vertical:hover {
    background: $text_muted;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: transparent;
}
QScrollBar:horizontal {
    background: transparent;
    height: 10px;
    margin: 2px;
}
QScrollBar::handle:horizontal {
    background: $border_strong;
    border-radius: 4px;
    min-width: 28px;
}
QScrollBar::handle:horizontal:hover {
    background: $text_muted;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0px;
}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
    background: transparent;
}
QStatusBar {
    background: $surface;
    border-top: 1px solid $border;
    color: $text_secondary;
}
QStatusBar::item {
    border: none;
}
QMenu {
    background: $elevated;
    border: 1px solid $border_strong;
    border-radius: 8px;
    padding: 5px;
}
QMenu::item {
    padding: 6px 26px 6px 12px;
    border-radius: 5px;
    color: $text;
}
QMenu::item:selected {
    background: $accent_soft;
    color: $text;
}
QMenu::separator {
    height: 1px;
    background: $border;
    margin: 4px 6px;
}
QToolTip {
    background: $elevated;
    color: $text;
    border: 1px solid $border_strong;
    padding: 5px 8px;
}
QMessageBox {
    background: $bg;
}
QFileDialog {
    background: $bg;
}
"""
)


def detect_system_mode() -> str:
    """返回 Windows 当前应用主题：``light`` 或 ``dark``。"""
    try:
        hints = QGuiApplication.styleHints()
        scheme = hints.colorScheme()
        if scheme == Qt.ColorScheme.Dark:
            return MODE_DARK
        if scheme == Qt.ColorScheme.Light:
            return MODE_LIGHT
    except Exception:  # noqa: BLE001 - 老版本 Qt 无 colorScheme
        pass
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            ) as key:
                value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
                return MODE_LIGHT if int(value) else MODE_DARK
        except (OSError, ValueError):
            pass
    return MODE_LIGHT


def install_chinese_translations(app: QApplication) -> None:
    """加载 Qt 自带中文翻译，使标准按钮与文件对话框显示中文。"""
    if getattr(app, "_dlr_qt_translator", None) is not None:
        return
    translator = QTranslator(app)
    path = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    if translator.load("qtbase_zh_CN", path):
        app.installTranslator(translator)
        app._dlr_qt_translator = translator  # type: ignore[attr-defined]
        logger.debug("已加载 Qt 中文翻译: %s", path)
    else:
        logger.debug("未找到 Qt 中文翻译，使用默认文案")


def apply_title_bar(widget: QWidget, dark: bool) -> None:
    """让 Windows 原生标题栏跟随深浅主题（尽力而为，不影响功能）。"""
    if sys.platform != "win32":
        return
    try:
        hwnd = int(widget.winId())
        if not hwnd:
            return
        value = ctypes.c_int(1 if dark else 0)
        applied = False
        for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE / 旧版
            result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)
            )
            if result == 0:
                applied = True
                break
        if not applied:
            return
        # 已显示的窗口需触发非客户区重绘，否则标题栏颜色不会立即更新。
        flags = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020  # FRAMECHANGED
        ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, flags)
        # Windows 11 的窗口边框也跟随主题；不支持时忽略返回值。
        border = ctypes.c_int(0x001A1715 if dark else -1)  # COLORREF (#15171A / 默认)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 34, ctypes.byref(border), ctypes.sizeof(border)
        )
    except Exception:  # noqa: BLE001 - 标题栏美化失败不应影响窗口
        logger.debug("设置标题栏深色模式失败", exc_info=True)


def app_is_dark() -> bool:
    """根据当前应用调色板判断是否处于深色主题。"""
    app = QApplication.instance()
    if app is None:
        return False
    return app.palette().color(QPalette.ColorRole.Window).lightness() < 128


class ThemeManager(QObject):
    """主题解析、应用与持久化。"""

    mode_changed = Signal(str)
    theme_changed = Signal(str)  # 实际生效的 light / dark

    def __init__(
        self,
        app: QApplication,
        database: Optional[Database] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._app = app
        self._database = database
        self._mode = MODE_SYSTEM
        self._effective = MODE_LIGHT
        self._load_mode()

        app.setStyle(QStyleFactory.create("Fusion"))
        font = QFont(UI_FONT_FAMILY)
        font.setPointSizeF(UI_FONT_SIZE_PT)
        app.setFont(font)

        hints = QGuiApplication.styleHints()
        try:
            hints.colorSchemeChanged.connect(self._on_system_scheme_changed)
        except AttributeError:
            logger.debug("当前 Qt 不支持 colorSchemeChanged 信号")

        # 对话框等运行期新建的顶层窗口在 showEvent 中单独同步标题栏，
        # 不使用全局事件过滤器，避免拖慢大量 Qt 事件的分发。
        self.apply()

    # ---------- 状态 ----------
    @property
    def mode(self) -> str:
        return self._mode

    @property
    def effective_mode(self) -> str:
        return self._effective

    @property
    def is_dark(self) -> bool:
        return self._effective == MODE_DARK

    @property
    def tokens(self) -> ThemeTokens:
        return DARK if self.is_dark else LIGHT

    def effective_label(self) -> str:
        label = MODE_LABELS.get(self._mode, MODE_LABELS[MODE_SYSTEM])
        if self._mode == MODE_SYSTEM:
            return f"{label} · {MODE_LABELS[self._effective]}"
        return label

    # ---------- 设置 ----------
    def set_mode(self, mode: str) -> None:
        if mode not in THEME_MODES:
            raise ValueError(f"未知主题模式: {mode}")
        if mode == self._mode:
            return
        self._mode = mode
        self._persist_mode()
        self.apply()
        self.mode_changed.emit(mode)
        logger.info("主题模式已切换为 %s", mode)

    def _load_mode(self) -> None:
        raw = ""
        if self._database is not None:
            try:
                raw = self._database.get_setting(THEME_SETTING_KEY, "")
            except Exception:  # noqa: BLE001 - 主题读取失败不得阻止启动
                logger.debug("读取主题设置失败", exc_info=True)
        self._mode = raw if raw in THEME_MODES else MODE_SYSTEM

    def _persist_mode(self) -> None:
        if self._database is None:
            return
        try:
            self._database.set_setting(THEME_SETTING_KEY, self._mode)
        except Exception:  # noqa: BLE001 - 持久化失败只影响下次启动
            logger.warning("保存主题设置失败", exc_info=True)

    # ---------- 应用 ----------
    def apply(self) -> None:
        self._effective = (
            detect_system_mode() if self._mode == MODE_SYSTEM else self._mode
        )
        tokens = self.tokens
        self._app.setPalette(self._palette(tokens))
        self._app.setStyleSheet(_QSS.substitute(tokens.__dict__))
        self._refresh_title_bars()
        self.theme_changed.emit(self._effective)

    def _on_system_scheme_changed(self, _scheme: object = None) -> None:
        if self._mode != MODE_SYSTEM:
            return
        before = self._effective
        self.apply()
        if before != self._effective:
            logger.info("系统主题变化，已自动切换为 %s", self._effective)

    def _refresh_title_bars(self) -> None:
        dark = self.is_dark
        for widget in QApplication.topLevelWidgets():
            if widget.isVisible():
                apply_title_bar(widget, dark)

    def refresh_title_bar(self, widget: QWidget) -> None:
        apply_title_bar(widget, self.is_dark)

    @staticmethod
    def _palette(tokens: ThemeTokens) -> QPalette:
        palette = QPalette()
        role = QPalette.ColorRole
        group = QPalette.ColorGroup
        palette.setColor(role.Window, QColor(tokens.bg))
        palette.setColor(role.WindowText, QColor(tokens.text))
        palette.setColor(role.Base, QColor(tokens.surface))
        palette.setColor(role.AlternateBase, QColor(tokens.surface_alt))
        palette.setColor(role.Text, QColor(tokens.text))
        palette.setColor(role.Button, QColor(tokens.surface))
        palette.setColor(role.ButtonText, QColor(tokens.text))
        palette.setColor(role.BrightText, QColor("#FFFFFF"))
        palette.setColor(role.Highlight, QColor(tokens.accent))
        palette.setColor(role.HighlightedText, QColor("#FFFFFF"))
        palette.setColor(role.ToolTipBase, QColor(tokens.elevated))
        palette.setColor(role.ToolTipText, QColor(tokens.text))
        palette.setColor(role.PlaceholderText, QColor(tokens.text_muted))
        palette.setColor(role.Link, QColor(tokens.accent_fg))
        palette.setColor(role.LinkVisited, QColor(tokens.accent_fg))
        palette.setColor(role.Mid, QColor(tokens.border))
        palette.setColor(role.Dark, QColor(tokens.border_strong))
        for disabled_role, color in (
            (role.WindowText, tokens.text_muted),
            (role.Text, tokens.text_muted),
            (role.ButtonText, tokens.text_muted),
            (role.Base, tokens.surface_alt),
            (role.Button, tokens.surface_alt),
        ):
            palette.setColor(group.Disabled, disabled_role, QColor(color))
        return palette
