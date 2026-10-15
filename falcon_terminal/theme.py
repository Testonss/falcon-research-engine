"""Falcon design tokens exposed to QML as the FalconTheme context object.

Single source of truth for colors, spacing, radii and type scale.
QML usage: `color: FalconTheme.bgPanel`.

Properties are declared explicitly: Qt registers them in the meta-object
at class creation, which dynamic setattr cannot do.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Property, Signal


def system_font_family() -> str:
    """Resolve the real system UI font; generic fallback headless.

    No bundled font files: Qt reports the platform UI font (Segoe UI on
    Windows). Offscreen/headless backends that cannot resolve fall back
    to a generic sans-serif instead of tofu boxes.
    """
    try:
        from PySide6.QtGui import QFontDatabase, QGuiApplication
        if QGuiApplication.instance() is not None:
            family = QFontDatabase.systemFont(
                QFontDatabase.SystemFont.GeneralFont).family()
            if family:
                return family
    except Exception:
        pass
    return "sans-serif"

TOKENS = {
    # Backgrounds
    "bgPrimary": "#0a0d13",
    "bgSecondary": "#0e1219",
    "bgPanel": "#131822",
    "bgPanelAlt": "#171d29",
    "bgHover": "#1b2230",
    "bgSelected": "#22303f",
    "bgInput": "#0c1017",
    # Borders
    "borderDefault": "#232c3a",
    "borderSubtle": "#1a2130",
    "borderActive": "#3a4a61",
    # Accent (Falcon gold/orange)
    "accentGold": "#f2a93b",
    "accentOrange": "#e07f2a",
    "accentDim": "#6e5230",
    # Semantic
    "positive": "#2ebd85",
    "negative": "#e5484d",
    "warning": "#f2c078",
    "info": "#5aa9ff",
    # Text
    "textPrimary": "#e9eef5",
    "textSecondary": "#9aa7b9",
    "textMuted": "#5f6d82",
    # Spacing
    "s4": 4, "s8": 8, "s12": 12, "s16": 16,
    "s20": 20, "s24": 24, "s32": 32,
    # Radii
    "r4": 4, "r6": 6, "r8": 8, "r10": 10,
    # Type
    "fontFamily": "Segoe UI",
    "displayPrice": 34, "pageTitle": 21, "panelTitle": 13,
    "assetTitle": 15, "metricValue": 15, "body": 12,
    "small": 11, "micro": 10, "tableHeader": 10,
}


def _const(value):
    return Property(type(value), fget=lambda self, v=value: v, constant=True)


class Theme(QObject):
    """Constant design tokens; no logic, no backend access."""

    familyChanged = Signal()

    def __init__(self, parent=None, family=None):
        super().__init__(parent)
        self._family = family or system_font_family()

    @Property(str, notify=familyChanged)
    def fontFamily(self):
        return self._family

    bgPrimary = _const("#0a0d13")
    bgSecondary = _const("#0e1219")
    bgPanel = _const("#131822")
    bgPanelAlt = _const("#171d29")
    bgHover = _const("#1b2230")
    bgSelected = _const("#22303f")
    bgInput = _const("#0c1017")
    borderDefault = _const("#232c3a")
    borderSubtle = _const("#1a2130")
    borderActive = _const("#3a4a61")
    accentGold = _const("#f2a93b")
    accentOrange = _const("#e07f2a")
    accentDim = _const("#6e5230")
    positive = _const("#2ebd85")
    negative = _const("#e5484d")
    warning = _const("#f2c078")
    info = _const("#5aa9ff")
    textPrimary = _const("#e9eef5")
    textSecondary = _const("#9aa7b9")
    textMuted = _const("#5f6d82")
    s4 = _const(4)
    s8 = _const(8)
    s12 = _const(12)
    s16 = _const(16)
    s20 = _const(20)
    s24 = _const(24)
    s32 = _const(32)
    r4 = _const(4)
    r6 = _const(6)
    r8 = _const(8)
    r10 = _const(10)
    displayPrice = _const(34)
    pageTitle = _const(21)
    panelTitle = _const(13)
    assetTitle = _const(15)
    metricValue = _const(15)
    body = _const(12)
    small = _const(11)
    micro = _const(10)
    tableHeader = _const(10)
