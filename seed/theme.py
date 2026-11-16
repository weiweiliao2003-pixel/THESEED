"""Validate data-only skins and render the desktop visual tokens."""

import hashlib
import json
import re
from pathlib import Path

from PySide6.QtGui import QColor, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication

STYLE = """
QWidget {
    color: #112638;
    font-family: @family;
    font-size: 14px;
}
QDialog, QMessageBox, QInputDialog { background: @dialog; }
QMenu, QComboBox QAbstractItemView {
    background: @menu; color: #112638; border: 1px solid #d7e2e7;
    selection-background-color: #1a644f; selection-color: @on_accent;
}
QMenu::item { padding: 7px 22px; }
QMenu::item:selected { background: #1a644f; color: @on_accent; }
QMenu::item:disabled, QCheckBox:disabled, QSpinBox:disabled { color: @disabled; }
QToolTip { background: @tooltip; color: #112638; font-family: @family; font-size: 13px;
    border: 1px solid #d7e2e7; padding: 5px; }
QSpinBox, QKeySequenceEdit {
    background: #fbfdfe; color: #112638; border: 1px solid #cbd9e0; border-radius: 6px; padding: 5px 8px;
}
QLineEdit, QPlainTextEdit, QComboBox { placeholder-text-color: #496270; }
QCheckBox { spacing: 7px; }
QCheckBox::indicator:unchecked { background: #fbfdfe; border: 1px solid #cbd9e0; border-radius: 3px; width: 13px; height: 13px; }
QCheckBox:focus, QSpinBox:focus, QKeySequenceEdit:focus { border: 1px solid @focus; }
QTabBar::tab { background: #ffffff; border: 1px solid #d7e2e7; border-radius: 7px; padding: 7px 12px; margin-right: 4px; }
QTabBar::tab:selected { background: #1a644f; color: @on_accent; }
QMainWindow, QWidget#root, QStackedWidget, QScrollArea, QScrollArea > QWidget > QWidget {
    background: #f1f5f7;
}
QScrollArea { border: none; }
QFrame#sidebar, QScrollArea#navScroll, QWidget#navigationPane { background: #132f40; }
QScrollArea#navScroll QScrollBar:vertical { background: #132f40; width: 7px; margin: 0; }
QScrollArea#navScroll QScrollBar::handle:vertical { background: @nav_divider; border-radius: 3px; min-height: 27px; }
QScrollArea#navScroll QScrollBar::add-line:vertical, QScrollArea#navScroll QScrollBar::sub-line:vertical {
    height: 0; background: transparent;
}
QLabel#navGroup { color: #f6fbfd; font-size: @nav_heading; font-weight: 700; padding: @nav_gap 10px 6px; }
QLabel#navGroup[currentGroup="true"] { color: @nav_marker; }
QFrame#navDivider { background: @nav_divider; max-height: 1px; }
QPushButton#navButton {
    background: transparent; color: #b5cbd5; text-align: left; font-size: @nav_item; font-weight: 400;
    border: 1px solid transparent; border-left-width: 3px; border-radius: 7px; padding: 0 16px;
}
QPushButton#navButton[collapsed="true"] { text-align: center; padding: 0; }
QPushButton#navButton:focus { border-style: dashed; border-color: @nav_focus; border-left-color: transparent; }
QPushButton#navButton:hover { color: #f6fbfd; background: #214556; border-left-color: @nav_marker; }
QPushButton#navButton[currentLocation="true"] { border-bottom: 2px solid @nav_marker; }
QLabel#pageLocation { color: #496270; padding: 5px 18px 3px; font-size: 12px; }
QFrame#resourceBlock { background: #ffffff; border: 1px solid #d7e2e7; border-radius: 8px; }
QLabel#resourceTitle { font-size: 17px; font-weight: 700; }
QLabel#resourceMetric { font-size: 15px; font-weight: 700; }
QLabel#resourceCaption { color: #496270; font-size: 12px; }
QPushButton#answerOption { text-align: left; }
QPushButton#answerOption:checked { background: #e4eef1; border-color: #1a644f; }
QProgressBar { background: #e4eef1; border: 1px solid #d7e2e7; border-radius: 4px; }
QProgressBar::chunk { background: #1a644f; border-radius: 3px; }
QLabel#pageTitle { font-size: 32px; font-weight: 700; color: #112638; }
QLabel#sectionTitle { font-size: 17px; font-weight: 700; color: #112638; }
QLabel#metric { font-size: 34px; font-weight: 600; color: #165845; }
QLabel#muted { color: #496270; }
QLabel#path { color: #1c604d; }
QFrame#card {
    background: #ffffff; border: 1px solid #d7e2e7; border-radius: 11px;
}
QPushButton {
    background: #ffffff; border: 1px solid #b7c9d2; border-radius: 6px;
    color: #18394b; padding: 7px 13px; font-weight: 600;
}
QPushButton:hover { background: #edf6f3; border-color: #7ea698; }
QPushButton:pressed { background: #dceee7; }
QPushButton:focus { border-color: #16735c; }
QPushButton[primary="true"] { color: #ffffff; background: #1a644f; border-color: #1a644f; }
QPushButton[primary="true"]:hover { background: #125441; }
QPushButton[primary="true"]:focus { border-color: #071e16; }
QPushButton:disabled, QPushButton[primary="true"]:disabled {
    color: #627682; background: #edf2f4; border-color: #d6e1e6;
}
QLineEdit, QPlainTextEdit, QComboBox, QListWidget, QTableWidget {
    background: #fbfdfe; border: 1px solid #cbd9e0; border-radius: 6px;
    selection-background-color: #d5eee4; selection-color: #14392d;
}
QLineEdit, QPlainTextEdit, QComboBox { padding: 7px 9px; }
QLineEdit:focus, QPlainTextEdit:focus, QListWidget:focus, QComboBox:focus, QTableWidget:focus {
    border-color: #458773;
}
QListWidget::item { padding: 8px; }
QHeaderView::section { background: #e4eef1; padding: 8px; border: none; }
QStatusBar { background: #e9f0f3; border-top: 1px solid #d7e2e7; color: #496270; }
QStatusBar QLabel { font-size: 12px; padding: 4px 10px; }
QFrame#topBar { border-bottom: 1px solid #d7e2e7; background: #132f40; }
QLabel#productTitle, QLabel#topStatus { color: #e1edf2; }
QLabel#productTitle { font-size: 24px; font-weight: 700; padding: 0 6px; }
QFrame#topBar QPushButton { background: transparent; color: #e1edf2; border-color: transparent; }
QFrame#topBar QPushButton:hover { background: @nav_hover; }
QFrame#conversationList, QScrollArea#contextDrawer, QWidget#contextBody { background: #e4eef1; }
QFrame#conversationList { border-right: 1px solid #d7e2e7; }
QFrame#identityHeader, QFrame#composer { background: #ffffff; border-bottom: 1px solid #d7e2e7; }
QFrame#composer { border: 1px solid #d7e2e7; border-radius: 13px; }
QFrame#composer QPlainTextEdit { border: none; background: #ffffff; }
QFrame#composer QPlainTextEdit:focus { border: 1px solid @focus; }
QFrame#composer QPushButton { font-weight: 500; }
QFrame#composer QPushButton[primary="true"] { min-width: 62px; font-weight: 600; }
QLabel#chatStatus { font-size: 12px; color: #496270; }
QFrame#chatMain, QFrame#emptyChat { background: #f1f5f7; }
QLabel#welcomeIcon { background: #e4eef1; border-radius: 36px; }
QLabel#chatName { font-size: 21px; font-weight: 700; }
QPushButton#avatar { background: #edf2f4; border-radius: 20px; padding: 0; }
QPushButton#quickPlus { background: #1a644f; color: @on_accent; font-size: 23px; border-radius: 20px; padding: 0; }
QTextBrowser#chatTimeline { background: #fbfdfe; padding: 16px 13px; }
QFrame#conversationList QListWidget { border: none; background: #e4eef1; }
QFrame#conversationList QListWidget::item { padding: 11px 9px; border-bottom: 1px solid #d7e2e7; }
QTextBrowser { border: none; background: #fbfdfe; }
"""

BASE = dict(background="#f3f9f4", panel="#ffffff", field="#f5f8f2", text="#1f2937", muted="#566373",
            line="#e6eceb", accent="#0e6b57", on_accent="#ffffff", nav="#0b4a3e", nav_text="#e1edf2",
            soft="#e3ede9", self_message="#baf0ca")
SKINS = {
    "forest": ("森林核心", BASE),
    "obsidian": ("曜石金", dict(background="#202122", panel="#292a2c", field="#252628", text="#f0ede6",
                muted="#cecabf", line="#59564e", accent="#e1be70", on_accent="#201b12", nav="#171819",
                nav_text="#f0e7d5", soft="#3d382d")),
    "nebula": ("星雲紫", dict(background="#f5f0fb", panel="#ffffff", field="#faf6ff", text="#2c2142",
               muted="#635478", line="#dbcee9", accent="#7042a6", on_accent="#ffffff", nav="#34204c",
               nav_text="#f1e7ff", soft="#e8dcf6")),
    "paper": ("極簡白", dict(background="#f6f7f8", panel="#ffffff", field="#ffffff", text="#14181d",
              muted="#434b56", line="#c6cdd6", accent="#254d76", on_accent="#ffffff", nav="#ebeff4",
              nav_text="#171f29", soft="#dce6f1")),
    "contrast": ("高對比", dict(background="#080808", panel="#101010", field="#101010", text="#ffffff",
                 muted="#eeeeee", line="#aaaaaa", accent="#ffe66b", on_accent="#111111", nav="#080808",
                 nav_text="#ffffff", soft="#303030")),
}
DEFAULT_SKIN = {"theme": "nebula", "font": 14, "radius": 8, "border": 1, "density": "normal",
                "icons": "line", "effects": False, "colors": {}}
FONT_CHOICES = {"noto": "Noto Sans TC", "huninn": "Huninn 粉圓", "system": "Windows 系統字體"}
WINDOWS_FAMILIES = '"Microsoft JhengHei UI", "Segoe UI"'


def ui_font(values):
    choice = values.get("typography", {}).get("family", "noto")
    if choice == "system":
        return WINDOWS_FAMILIES
    application = QApplication.instance()
    source = {"noto": ("NotoSansTC.ttf", "Noto Sans TC"), "huninn": ("Huninn-Regular.ttf", "Huninn")}
    loaded_families = []
    for key in ("huninn", "noto") if choice == "huninn" else ("noto",):
        filename, preferred = source[key]
        prop = f"theseedFont_{key}"
        family = application.property(prop)
        if family is None:
            file = Path(__file__).with_name("resources") / "fonts" / filename
            loaded = QFontDatabase.addApplicationFont(str(file))
            families = QFontDatabase.applicationFontFamilies(loaded) if loaded >= 0 else []
            family = preferred if preferred in families else (families[0] if families else "")
            application.setProperty(prop, family)
        if family:
            loaded_families.append(f'"{family}"')
    return ", ".join([*loaded_families, WINDOWS_FAMILIES])


def validate_skin(values):
    if not isinstance(values, dict) or values.keys() - {"navigation", "typography"} != DEFAULT_SKIN.keys():
        raise ValueError("主題欄位不相容。")
    for key, allowed in {"theme": SKINS, "density": ("normal", "compact"), "icons": ("line", "none")}.items():
        if not isinstance(values[key], str) or values[key] not in allowed:
            raise ValueError("主題選項無效。")
    for key, (low, high) in {"font": (12, 22), "radius": (0, 16), "border": (1, 3)}.items():
        if type(values[key]) is not int or not low <= values[key] <= high:
            raise ValueError("主題尺寸超出範圍。")
    colors = values["colors"]
    if (type(values["effects"]) is not bool or not isinstance(colors, dict) or not colors.keys() <= BASE.keys()
            or any(not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color)
                   for color in colors.values())):
        raise ValueError("主題只接受受控色碼與選項。")
    if "navigation" in values:
        nav = values["navigation"]
        if (not isinstance(nav, dict) or nav.keys() != {"font", "group_gap"} or
                type(nav["font"]) is not int or not 13 <= nav["font"] <= 20 or
                type(nav["group_gap"]) is not int or not 8 <= nav["group_gap"] <= 28):
            raise ValueError("導覽字級或分區間距超出範圍。")
    if "typography" in values:
        typography = values["typography"]
        if (not isinstance(typography, dict) or typography.keys() - {"family"} != {"weight", "line_height"}
                or typography["weight"] not in {"normal", "medium"} or type(typography["line_height"]) is not int
                or not 100 <= typography["line_height"] <= 160 or typography.get("family", "noto") not in FONT_CHOICES):
            raise ValueError("字體或行距設定不相容。")
    return values


def theme_document(values):
    values = validate_skin(values)
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    return {"schema": "seed.skin/1", "settings": values, "sha256": hashlib.sha256(encoded).hexdigest()}


def read_skin(path):
    with path.open("rb") as handle:
        data = handle.read(16_385)
    if len(data) > 16_384:
        raise ValueError("主題檔過大。")
    document = json.loads(data.decode("utf-8-sig"))
    if not isinstance(document, dict) or document.get("schema") != "seed.skin/1":
        raise ValueError("主題版本不相容。")
    if document != theme_document(document.get("settings")):
        raise ValueError("主題指紋不符或包含不受支援的資料。")
    return document["settings"]


def colors_for(values):
    return SKINS[values["theme"]][1] | values["colors"]


def apply_skin(window, values):
    values = validate_skin(values)
    colors = colors_for(values)
    colors |= {"dialog": colors["panel"], "menu": colors["panel"], "tooltip": colors["panel"],
               "disabled": colors["muted"], "focus": colors["accent"], "family": ui_font(values)}
    nav, text = QColor(colors["nav"]), QColor(colors["nav_text"])
    def blended(ratio):
        return QColor(*(round(base + (value - base) * ratio) for base, value in
                        zip(nav.getRgb()[:3], text.getRgb()[:3], strict=True))).name()
    nav_font = navigation_token(values, "font")
    colors.update(nav_divider=blended(.26), nav_hover=blended(.16), nav_child=blended(.82),
                  nav_marker=blended(.95), nav_focus=blended(.80),
                  nav_heading=f"{nav_font+4}px", nav_item=f"{nav_font}px",
                  nav_gap=f"{navigation_token(values, 'group_gap')}px")
    source_colors = {
        "#f1f5f7": "background", "#ffffff": "panel", "#fbfdfe": "field", "#112638": "text",
        "#496270": "muted", "#d7e2e7": "line", "#1a644f": "accent", "#132f40": "nav",
        "#e1edf2": "nav_text", "#f6fbfd": "nav_text", "#b5cbd5": "nav_child", "#214556": "nav_hover",
        "#285548": "accent", "#3e7566": "accent", "#18394b": "text", "#b7c9d2": "line",
        "#edf6f3": "soft", "#edf2f4": "soft", "#cbd9e0": "line", "#e4eef1": "soft",
        "#e9f0f3": "background", "#d5eee4": "soft", "#14392d": "text", "#165845": "text",
        "#1c604d": "text", "#627682": "muted", "#125441": "accent", "#dceee7": "soft",
        "#458773": "focus", "#16735c": "focus", "#071e16": "focus", "#7ea698": "line",
        "#a5dbc7": "focus", "#d6e1e6": "line",
    }
    template = STYLE.replace('color: #ffffff; background: #1a644f', 'color: @on_accent; background: #1a644f')
    template = template.replace('color: #ffffff; background: #285548', 'color: @on_accent; background: #285548')
    template = re.sub(r"#[0-9a-f]{6}|@[a-z_]+",
                      lambda match: colors.get(source_colors.get(match[0], match[0].lstrip("@")), match[0]), template)
    if values.get("typography", {}).get("weight") == "medium":
        template = template.replace("font-size: 14px;\n}", "font-size: 14px; font-weight: 500;\n}", 1)
    template = re.sub(r"font-size: (\d+)px", lambda m: f"font-size: {int(m[1]) + values['font'] - 14}px", template)
    template = re.sub(r"border-radius: \d+px", f"border-radius: {values['radius']}px", template)
    template = template.replace("1px solid", f"{values['border']}px solid")
    if values["density"] == "compact":
        template = template.replace("padding: 7px", "padding: 4px").replace("padding: 8px", "padding: 5px")
    template += "\nQLabel#welcomeIcon { border-radius: 36px; }"
    template += ('\nQPushButton#windowControl { min-width: 25px; padding: 5px 4px; font-size: 16px; }'
                 '\nQPushButton#windowControl[closeWindow="true"]:hover { background: #b93e53; color: white; }'
                 '\nQPushButton#resourceRailButton { padding: 6px 0; min-width: 0; }')
    template += (f'\nQScrollArea#attachmentStrip {{ border: 0; }}'
                 f'\nQWidget#attachmentChip {{ background: {colors["soft"]}; border-radius: {values["radius"]}px; }}'
                 '\nQPushButton#attachmentRemove { padding: 3px 5px; min-width: 15px; }')
    application = QApplication.instance()
    if application.styleSheet() == template:
        window.setPalette(application.palette())
        return
    palette = QPalette(application.style().standardPalette())
    for role, color in ((QPalette.ColorRole.Window, "dialog"), (QPalette.ColorRole.Base, "field"),
                        (QPalette.ColorRole.AlternateBase, "soft"), (QPalette.ColorRole.Text, "text"),
                        (QPalette.ColorRole.WindowText, "text"), (QPalette.ColorRole.Button, "panel"),
                        (QPalette.ColorRole.ButtonText, "text"), (QPalette.ColorRole.PlaceholderText, "muted"),
                        (QPalette.ColorRole.ToolTipBase, "tooltip"), (QPalette.ColorRole.ToolTipText, "text"),
                        (QPalette.ColorRole.Link, "accent"), (QPalette.ColorRole.Highlight, "accent"),
                        (QPalette.ColorRole.HighlightedText, "on_accent")):
        palette.setColor(role, QColor(colors[color]))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(colors["disabled"]))
    application.setPalette(palette)
    application.setStyleSheet(template)
    window.setPalette(palette)


def navigation_token(skin, key):
    return skin.get("navigation", {}).get(key, {"font": 15, "group_gap": 16}[key])
