"""主题 / 视觉规范的唯一来源（SSoT）

约定（CLAUDE.md 硬约束 15）：
- 所有颜色、圆角、间距、字号都来自这里
- 其他文件不许 hardcode 任何 hex 颜色字面量
- 唯一例外：src/ui/syntax_highlighter.py 是 Pygments 代码语法色板（语义独立）

风格定调：GitHub Light / GitHub Dark 双主题。
- 两套主题都清晰区分窗口、导航、内容、卡片和代码区。
- 蓝色只用于当前项、主操作和焦点；状态色只表达 Git/运行风险。
- 保持桌面 IDE 的信息密度，不使用营销式大卡片或装饰性渐变。
"""
from __future__ import annotations

import re

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QLineEdit


THEME_GITHUB_DARK = "github-dark"
THEME_GITHUB_LIGHT = "github-light"
THEME_NAMES = {
    THEME_GITHUB_DARK: "GitHub Dark",
    THEME_GITHUB_LIGHT: "GitHub Light",
}
DEFAULT_THEME = THEME_GITHUB_DARK
_current_theme = DEFAULT_THEME


class ThemeColor(str):
    """可原地换值的颜色 token，保持既有 `from theme import COLOR` 引用有效。"""

    def __new__(cls, value: str):
        obj = super().__new__(cls, value)
        obj._value = value
        return obj

    def set(self, value: str) -> None:
        self._value = value

    def __str__(self) -> str:
        return self._value

    def __format__(self, format_spec: str) -> str:
        return format(self._value, format_spec)


def color_value(value) -> str:
    """给 QColor 等 C++ API 使用，避免 str 子类保留初始底层值。"""
    return str(value)


def _color(value: str) -> ThemeColor:
    return ThemeColor(value)


# ====================================================================
# Color tokens
# ====================================================================

# 背景层级（从底到顶）。
# Darcula 的层级感比 Tokyo Night 弱——主窗口/侧栏/工具栏全部 #3C3F41，
# 只有编辑器/日志区单独用 #2B2B2B 以突出"代码内容"。
BG_L0 = _color("#0D1117")   # 顶部标签栏、状态栏
BG_L1 = _color("#161B22")   # 主窗口背景
BG_L2 = _color("#21262D")   # 导航、面板、对话框
BG_L3 = _color("#30363D")   # 输入框、卡片、表头
BG_L4 = _color("#3C444D")   # hover 高亮
BG_L5 = _color("#1F6FEB")   # pressed / 选中
BG_CONTENT = _color("#161B22")  # 主内容面
BG_CARD = _color("#30363D")     # 卡片面

# 代码 / 日志区底色：Darcula 编辑器经典 #2B2B2B
BG_CODE = _color("#0C1016")

# 边框
BORDER_SUBTLE = _color("#30363E")   # 普通边框
BORDER_STRONG = _color("#484F58")   # 分隔强调
BORDER_FOCUS = _color("#58A6FF")    # 焦点蓝

# 文字（4 档亮度，区分主次）
# Darcula 原版 FG #BBBBBB 在 #3C3F41 底上对比度 ~5.7:1，长时间盯着费眼；
# 这里整体提亮一档到 ~8.5:1，但仍保留"米灰"调（不上纯白避免刺眼）。
FG_PRIMARY = _color("#C9D1D9")      # 主文字
FG_SECONDARY = _color("#8B949E")    # 次级文字
FG_DIM = _color("#6E7681")          # 占位符 / disabled / 注释
FG_BRIGHT = _color("#F0F6FC")       # 选中项 / 高亮区域文字
FG_ON_ACCENT = _color("#FFFFFF")    # 彩色主按钮上的文字

# 强调色（accent，IDEA 蓝）
ACCENT = _color("#2F81F7")
ACCENT_HOVER = _color("#57A5FE")
ACCENT_SUBTLE = _color("#1F3B61")
ACCENT_PRESSED = _color("#1E6EEA")

# 语义色 —— 跨 widget 一致使用（Darcula 语法着色）
COLOR_SUCCESS = _color("#3FB950")
COLOR_WARN = _color("#D29922")
COLOR_ERROR = _color("#F85149")
COLOR_INFO = _color("#58A6FE")
COLOR_DEBUG = _color("#8A939D")
COLOR_SQL = _color("#D2A8FF")
COLOR_BANNER = _color("#D1A7FE")
COLOR_LINK = _color("#58A5FF")

# 应用内轻提示（替代系统右下角通知）
TOAST_INFO_BG = _color("#1E3A60")
TOAST_INFO_BORDER = COLOR_INFO
TOAST_SUCCESS_BG = _color("#1A4620")
TOAST_SUCCESS_BORDER = COLOR_SUCCESS
TOAST_ERROR_BG = _color("#552322")
TOAST_ERROR_BORDER = COLOR_ERROR

# 状态点三态
DOT_IDLE = _color("#6D7580")
DOT_RUNNING = _color("#3EB84F")
DOT_WARN = _color("#D19821")

# Git 相关（diff 着色 / 状态文件名）—— IDEA Git 视图配色
GIT_ADD = _color("#40BA51")
GIT_DEL = _color("#F75048")
GIT_MODIFY = _color("#D39A23")
GIT_HUNK = _color("#57A6FF")
GIT_FILE_HEAD = _color("#D3A9FF")
GIT_META = _color("#8C959F")
GIT_CONFLICT = _color("#DB6D28")
GIT_IGNORED = _color("#6F7782")

# 诊断条（醒目的"出问题了"通知背景）
BG_DIAGNOSIS = _color("#3B2E12")
BORDER_DIAGNOSIS = _color("#9E6A03")

# 搜索命中高亮（IDEA find usages 用的暗绿底 + 白字）
HIGHLIGHT_MATCH_BG = _color("#264F78")
HIGHLIGHT_MATCH_FG = _color("#EFF5FB")

# 按钮配色：通用、primary、danger、success
# Darcula 普通按钮 #4C5052 灰，主按钮 #365880 蓝（OK / Apply 那种）
BG_BTN = _color("#20252C")
BG_BTN_HOVER = _color("#30363F")
BG_BTN_PRESSED = _color("#151A21")
BG_BTN_PRIMARY = _color("#238636")
BG_BTN_PRIMARY_HOVER = _color("#2EA043")
BG_BTN_DANGER = _color("#3D1F24")
BG_BTN_DANGER_HOVER = _color("#5A2428")
BG_BTN_SUCCESS = _color("#1C4822")
BG_BTN_SUCCESS_HOVER = _color("#216E39")


_THEME_PALETTES = {
    THEME_GITHUB_DARK: {
        "BG_L0": "#0D1117", "BG_L1": "#161B22", "BG_L2": "#21262D",
        "BG_L3": "#30363D", "BG_L4": "#3C444D", "BG_L5": "#1F6FEB",
        "BG_CONTENT": "#161B22", "BG_CARD": "#30363D",
        "BG_CODE": "#0C1016", "BORDER_SUBTLE": "#30363E",
        "BORDER_STRONG": "#484F58", "BORDER_FOCUS": "#58A6FF",
        "FG_PRIMARY": "#C9D1D9", "FG_SECONDARY": "#8B949E",
        "FG_DIM": "#6E7681", "FG_BRIGHT": "#F0F6FC", "FG_ON_ACCENT": "#FFFFFF",
        "ACCENT": "#2F81F7", "ACCENT_HOVER": "#57A5FE",
        "ACCENT_SUBTLE": "#1F3B61", "ACCENT_PRESSED": "#1E6EEA",
        "COLOR_SUCCESS": "#3FB950", "COLOR_WARN": "#D29922",
        "COLOR_ERROR": "#F85149", "COLOR_INFO": "#58A6FE",
        "COLOR_DEBUG": "#8A939D", "COLOR_SQL": "#D2A8FF",
        "COLOR_BANNER": "#D1A7FE", "COLOR_LINK": "#58A5FF",
        "TOAST_INFO_BG": "#1E3A60", "TOAST_SUCCESS_BG": "#1A4620",
        "TOAST_ERROR_BG": "#552322", "DOT_IDLE": "#6D7580",
        "DOT_RUNNING": "#3EB84F", "DOT_WARN": "#D19821",
        "GIT_ADD": "#40BA51", "GIT_DEL": "#F75048", "GIT_MODIFY": "#D39A23",
        "GIT_HUNK": "#57A6FF", "GIT_FILE_HEAD": "#D3A9FF",
        "GIT_META": "#8C959F", "GIT_CONFLICT": "#DB6D28",
        "GIT_IGNORED": "#6F7782", "BG_DIAGNOSIS": "#3B2E12",
        "BORDER_DIAGNOSIS": "#9E6A03", "HIGHLIGHT_MATCH_BG": "#264F78",
        "HIGHLIGHT_MATCH_FG": "#EFF5FB", "BG_BTN": "#20252C",
        "BG_BTN_HOVER": "#30363F", "BG_BTN_PRESSED": "#151A21",
        "BG_BTN_PRIMARY": "#238636", "BG_BTN_PRIMARY_HOVER": "#2EA043",
        "BG_BTN_DANGER": "#3D1F24", "BG_BTN_DANGER_HOVER": "#5A2428",
        "BG_BTN_SUCCESS": "#1C4822", "BG_BTN_SUCCESS_HOVER": "#216E39",
    },
    THEME_GITHUB_LIGHT: {
        "BG_L0": "#F0F2F4", "BG_L1": "#F6F8FA", "BG_L2": "#FFFFFF",
        "BG_L3": "#F1F3F5", "BG_L4": "#E7EBEF", "BG_L5": "#D8DEE4",
        "BG_CONTENT": "#FFFFFF", "BG_CARD": "#FFFFFF",
        "BG_CODE": "#FFFFFF", "BORDER_SUBTLE": "#D8DEE4",
        "BORDER_STRONG": "#B7C0CA", "BORDER_FOCUS": "#0969DA",
        "FG_PRIMARY": "#1F2328", "FG_SECONDARY": "#59636E",
        "FG_DIM": "#6E7781", "FG_BRIGHT": "#1F2328", "FG_ON_ACCENT": "#FFFFFF",
        "ACCENT": "#0969DA", "ACCENT_HOVER": "#0550AE",
        "ACCENT_SUBTLE": "#E7F1FA", "ACCENT_PRESSED": "#0349B4",
        "COLOR_SUCCESS": "#1A7F37", "COLOR_WARN": "#8A5D00",
        "COLOR_ERROR": "#CF222E", "COLOR_INFO": "#0969DA",
        "COLOR_DEBUG": "#6E7781", "COLOR_SQL": "#8250DF",
        "COLOR_BANNER": "#8250DF", "COLOR_LINK": "#0969DA",
        "TOAST_INFO_BG": "#DDF4FF", "TOAST_SUCCESS_BG": "#DAFBE1",
        "TOAST_ERROR_BG": "#FFEBE9", "DOT_IDLE": "#6E7781",
        "DOT_RUNNING": "#1A7F37", "DOT_WARN": "#8A5D00",
        "GIT_ADD": "#1A7F37", "GIT_DEL": "#CF222E", "GIT_MODIFY": "#8A5D00",
        "GIT_HUNK": "#0969DA", "GIT_FILE_HEAD": "#8250DF",
        "GIT_META": "#59636E", "GIT_CONFLICT": "#BC4C00",
        "GIT_IGNORED": "#8C959F", "BG_DIAGNOSIS": "#FFF8C5",
        "BORDER_DIAGNOSIS": "#D4A72C", "HIGHLIGHT_MATCH_BG": "#B6E3FF",
        "HIGHLIGHT_MATCH_FG": "#1F2328", "BG_BTN": "#F6F8FA",
        "BG_BTN_HOVER": "#EAEEF2", "BG_BTN_PRESSED": "#D8DEE4",
        "BG_BTN_PRIMARY": "#1F883D", "BG_BTN_PRIMARY_HOVER": "#187D35",
        "BG_BTN_DANGER": "#FFF1F0", "BG_BTN_DANGER_HOVER": "#CF222E",
        "BG_BTN_SUCCESS": "#DAFBE1", "BG_BTN_SUCCESS_HOVER": "#ACEEBB",
    },
}


# ====================================================================
# 兼容别名（旧代码用的常量名，保留方便渐进迁移）
# ====================================================================
# 等阶段 5 全切完后这一段可以删掉
ACCENT_LIGHT = ACCENT_HOVER
BG_DARKER = BG_L2
BG_PANEL = BG_L2
BG_HOVER = BG_L4
BORDER = BORDER_SUBTLE
TEXT_PRIMARY = FG_PRIMARY
TEXT_SECONDARY = FG_SECONDARY
COLOR_NEUTRAL = FG_DIM


# ====================================================================
# 形状 / 间距 / 字号
# ====================================================================

RADIUS_SM = 4
RADIUS_MD = 6
RADIUS_LG = 8

GAP_NONE = 0
GAP_XS = 4
GAP_SM = 6
GAP_MD = 12
GAP_LG = 20

# Widget 高度
H_BTN = 30
H_BTN_SM = 22
H_TOOLBAR = 36
H_STATUSBAR = 24
H_TABLE_ROW = 32
W_LEFT_PANEL = 260
W_DIALOG_WIDE = 920
H_DIALOG_TALL = 680

# 字号（point）
TREE_ITEM_PAD_V = 3        # 树/列表行的上下内边距
FONT_PT_UI = 10            # UI 主字号（Qt 默认 9 偏小）
FONT_PT_UI_SM = 9          # 副标题 / 次级文字
FONT_PT_UI_LG = 12         # 标题
FONT_PT_CODE = 13          # 编辑器 / 日志
FONT_PT_DIFF = 11          # diff 视图

# 字体族
FONT_FAMILY_UI = '"Segoe UI", "Microsoft YaHei UI", system-ui, sans-serif'
FONT_FAMILY_CODE = '"Cascadia Mono", "Consolas", "Courier New", monospace'


# ====================================================================
# 全局 base QSS（自写，不依赖 qdarkstyle）
# ====================================================================

def _base_qss() -> str:
    """返回完整 base QSS。所有颜色 / 间距走上面的 token，改这里一次全应用生效。"""
    return f"""
/* ===== 顶层 ===== */
QMainWindow, QWidget {{
    background: {BG_L1};
    color: {FG_PRIMARY};
    font-family: {FONT_FAMILY_UI};
    font-size: {FONT_PT_UI}pt;
}}

QFrame {{
    background: transparent;
    color: {FG_PRIMARY};
}}

QDialog {{
    background: {BG_L2};
    color: {FG_PRIMARY};
}}

QWidget#project_workbench {{
    background: {BG_CONTENT};
}}

/* ===== 文字 ===== */
QLabel {{
    background: transparent;
    color: {FG_PRIMARY};
}}
QLabel[role="title"] {{
    color: {FG_PRIMARY};
    font-size: {FONT_PT_UI_LG}pt;
    font-weight: 600;
}}
QLabel[role="subtitle"] {{
    color: {FG_SECONDARY};
    font-size: {FONT_PT_UI_SM}pt;
}}
QLabel[role="hint"] {{
    color: {FG_DIM};
    font-size: {FONT_PT_UI_SM}pt;
}}
QLabel[role="workspace-status-ok"] {{ color: {COLOR_SUCCESS}; }}
QLabel[role="workspace-status-warn"] {{ color: {COLOR_WARN}; }}
QLabel[role="workspace-status-error"] {{ color: {COLOR_ERROR}; }}

/* ===== 按钮 ===== */
QPushButton {{
    background: {BG_BTN};
    color: {FG_PRIMARY};
    border: 1px solid {BORDER_SUBTLE};
    border-radius: {RADIUS_SM}px;
    padding: 5px 14px;
    min-height: {H_BTN - 10}px;
}}
QPushButton:hover {{ background: {BG_BTN_HOVER}; border-color: {BORDER_STRONG}; }}
QPushButton:pressed {{ background: {BG_BTN_PRESSED}; }}
QPushButton:disabled {{ background: {BG_L2}; color: {FG_DIM}; border-color: {BORDER_SUBTLE}; }}
QPushButton:focus {{ border-color: {BORDER_FOCUS}; }}

QPushButton#external_tool_button {{
    background: {BG_L2};
    border-color: {BORDER_SUBTLE};
    font-weight: 600;
    padding-left: 12px;
    padding-right: 12px;
}}
QPushButton[compact="true"], QPushButton#external_tool_button[compact="true"] {{
    padding: {GAP_XS}px {GAP_SM}px;
    min-height: {H_BTN_SM - GAP_XS * 2}px;
}}
QToolButton[compact="true"] {{
    padding: {GAP_NONE}px;
    border-color: transparent;
}}
QToolButton[compact="true"]:hover {{ border-color: {BORDER_SUBTLE}; }}
QPushButton#external_tool_button:hover {{
    background: {ACCENT_SUBTLE};
    border-color: {ACCENT};
    color: {COLOR_LINK};
}}

QPushButton[role="primary"] {{
    background: {BG_BTN_PRIMARY};
    color: {FG_ON_ACCENT};
    border: 1px solid {BG_BTN_PRIMARY};
}}
QPushButton[role="primary"]:hover {{ background: {BG_BTN_PRIMARY_HOVER}; border-color: {BG_BTN_PRIMARY_HOVER}; }}
QPushButton[role="primary"]:pressed {{ background: {ACCENT_PRESSED}; }}
QPushButton[role="primary"]:disabled {{ background: {BG_L4}; color: {FG_DIM}; border-color: {BORDER_SUBTLE}; }}

QPushButton[role="danger"] {{
    background: {BG_BTN_DANGER};
    color: {COLOR_ERROR};
    border: 1px solid {COLOR_ERROR};
}}
QPushButton[role="danger"]:hover {{ background: {BG_BTN_DANGER_HOVER}; border-color: {BG_BTN_DANGER_HOVER}; color: {FG_ON_ACCENT}; }}
QPushButton[role="danger"]:disabled {{ background: {BG_L2}; color: {FG_DIM}; border-color: {BORDER_SUBTLE}; }}

QPushButton[role="success"] {{
    background: {BG_BTN_SUCCESS};
    color: {COLOR_SUCCESS};
    border: 1px solid {BG_BTN_SUCCESS};
}}
QPushButton[role="success"]:hover {{ background: {BG_BTN_SUCCESS_HOVER}; }}

/* ===== ToolButton（紧凑型按钮）===== */
QToolButton {{
    background: {BG_BTN};
    color: {FG_PRIMARY};
    border: 1px solid {BORDER_SUBTLE};
    border-radius: {RADIUS_SM}px;
    padding: 4px 9px;
    min-height: {H_BTN - 10}px;
}}
QToolButton:hover {{ background: {BG_BTN_HOVER}; border-color: {BORDER_STRONG}; }}
QToolButton:pressed {{ background: {BG_BTN_PRESSED}; }}
QToolButton:focus {{ border-color: {BORDER_FOCUS}; }}
QToolButton:disabled {{ color: {FG_DIM}; background: {BG_L2}; border-color: {BORDER_SUBTLE}; }}
QToolButton::menu-indicator {{ image: none; width: 0; }}
QToolButton[role="primary"] {{
    background: {BG_BTN_PRIMARY};
    color: {FG_ON_ACCENT};
    border-color: {BG_BTN_PRIMARY};
}}
QToolButton[role="primary"]:hover {{
    background: {BG_BTN_PRIMARY_HOVER};
    border-color: {BG_BTN_PRIMARY_HOVER};
}}
QToolButton[role="primary"]:pressed {{ background: {ACCENT_PRESSED}; }}
QToolButton[role="primary"]:disabled {{ background: {BG_L4}; color: {FG_DIM}; border-color: {BORDER_SUBTLE}; }}
QToolButton[role="danger"] {{
    background: {BG_BTN_DANGER};
    color: {COLOR_ERROR};
    border-color: {COLOR_ERROR};
}}
QToolButton[role="danger"]:hover {{
    background: {BG_BTN_DANGER_HOVER};
    color: {FG_ON_ACCENT};
    border-color: {BG_BTN_DANGER_HOVER};
}}
QToolButton[role="danger"]:disabled {{ background: {BG_L2}; color: {FG_DIM}; border-color: {BORDER_SUBTLE}; }}

/* ===== 输入框 ===== */
QLineEdit {{
    background: {BG_L3};
    color: {FG_PRIMARY};
    border: 1px solid {BORDER_SUBTLE};
    border-radius: {RADIUS_SM}px;
    padding: 4px 8px;
    selection-background-color: {ACCENT_SUBTLE};
    selection-color: {FG_BRIGHT};
}}
QLineEdit:focus {{ border-color: {BORDER_FOCUS}; }}
QLineEdit:disabled {{ color: {FG_DIM}; background: {BG_L2}; }}
QLineEdit[placeholderText] {{ color: {FG_DIM}; }}

/* ===== 搜索框（全项目搜索 / 文件树过滤 / 日志搜索 / Ctrl+F）=====
   高对比白字 + 等宽字体 + 加大字号 + 加深底色，让用户输入的查询词醒目突出。
   通过 widget setProperty("role", "search") 应用，或调 apply_search_style() 一并
   把 placeholder 颜色提亮（QSS 选择器改不了 placeholder，需走 palette）。 */
QLineEdit[role="search"] {{
    background: {BG_L1};
    color: {FG_BRIGHT};
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS_SM}px;
    padding: 6px 10px;
    font-family: {FONT_FAMILY_CODE};
    font-size: {FONT_PT_UI_LG}pt;
    selection-background-color: {ACCENT_SUBTLE};
    selection-color: {FG_BRIGHT};
}}
QLineEdit[role="search"]:focus {{ border-color: {BORDER_FOCUS}; }}
QLineEdit[role="search"]:disabled {{ color: {FG_DIM}; background: {BG_L2}; }}

/* ===== 代码 / 日志区 ===== */
QPlainTextEdit, QTextEdit {{
    background-color: {BG_CODE};
    color: {FG_PRIMARY};
    border: 1px solid {BORDER_SUBTLE};
    selection-background-color: {ACCENT_SUBTLE};
    selection-color: {FG_BRIGHT};
    font-family: {FONT_FAMILY_CODE};
    font-size: {FONT_PT_CODE}pt;
}}
QPlainTextEdit:focus, QTextEdit:focus {{ border-color: {BORDER_FOCUS}; }}

/* ===== 树 / 列表 ===== */
QTreeView, QTreeWidget, QListView, QListWidget {{
    background: {BG_L2};
    color: {FG_PRIMARY};
    border: none;
    outline: none;
    alternate-background-color: {BG_L2};
}}
QTreeView::item, QTreeWidget::item, QListView::item, QListWidget::item {{
    padding: {TREE_ITEM_PAD_V}px 4px;
    border: none;
}}
QTreeView::item:hover, QTreeWidget::item:hover, QListView::item:hover, QListWidget::item:hover {{
    background: {BG_L4};
}}
QTreeView::item:selected, QTreeWidget::item:selected, QListView::item:selected, QListWidget::item:selected {{
    background: {ACCENT_SUBTLE};
    color: {FG_BRIGHT};
}}

/* ===== Tab ===== */
QTabWidget::pane {{
    border: none;
    border-top: 1px solid {BORDER_SUBTLE};
    background: {BG_CONTENT};
    top: -1px;
}}
QTabBar {{
    background: {BG_L0};
    qproperty-drawBase: 0;
}}
QTabBar::tab {{
    background: {BG_L0};
    color: {FG_SECONDARY};
    padding: 8px 18px;
    border: none;
    border-right: 1px solid {BORDER_SUBTLE};
    min-width: 80px;
    font-size: {FONT_PT_UI}pt;
}}
QTabBar::tab:hover:!selected {{
    background: {BG_L2};
    color: {FG_PRIMARY};
}}
QTabBar::tab:selected {{
    background: {BG_L2};
    color: {FG_BRIGHT};
    border-bottom: 3px solid {ACCENT};
}}
QTabBar::close-button {{
    subcontrol-position: right;
    border-radius: {RADIUS_SM}px;
    margin: 2px;
}}
QTabBar::close-button:hover {{ background: {BG_BTN_DANGER}; }}

/* ===== 菜单 ===== */
QMenuBar {{
    background: {BG_L2};
    color: {FG_PRIMARY};
    border-bottom: 1px solid {BORDER_SUBTLE};
}}
QMenuBar::item {{
    background: transparent;
    padding: 4px 10px;
}}
QMenuBar::item:selected {{ background: {BG_L4}; }}

QMenu {{
    background: {BG_L2};
    color: {FG_PRIMARY};
    border: 1px solid {BORDER_STRONG};
    padding: 4px;
}}
QMenu::item {{
    padding: 5px 18px;
    border-radius: {RADIUS_SM}px;
}}
QMenu::item:selected {{ background: {ACCENT_SUBTLE}; color: {FG_BRIGHT}; }}
QMenu::item:disabled {{ color: {FG_DIM}; }}
QMenu::separator {{
    height: 1px;
    background: {BORDER_SUBTLE};
    margin: 4px 6px;
}}

/* ===== 状态栏 ===== */
QStatusBar {{
    background: {BG_L0};
    color: {FG_SECONDARY};
    border-top: 1px solid {BORDER_SUBTLE};
    min-height: {H_STATUSBAR}px;
}}
QStatusBar::item {{ border: none; }}

/* ===== 需求工作区卡片 ===== */
QScrollArea#workspace_scroll {{
    background: transparent;
    border: none;
}}
QScrollArea#workspace_scroll > QWidget > QWidget {{
    background: transparent;
}}
QFrame#workspace_card {{
    background: {BG_L2};
    border: 1px solid {BORDER_SUBTLE};
    border-radius: {RADIUS_MD}px;
}}
QFrame#workspace_card:hover {{
    background: {BG_L4};
    border-color: {BORDER_STRONG};
}}
QFrame#workspace_card[current="true"] {{
    border-color: {ACCENT};
    border-left: 3px solid {ACCENT};
}}
QFrame#workspace_card[selected="true"] {{
    border-color: {ACCENT_HOVER};
}}
QLabel#workspace_card_title {{
    color: {FG_BRIGHT};
    font-size: {FONT_PT_UI_LG}pt;
    font-weight: 600;
}}
QLabel#workspace_current {{
    color: {COLOR_LINK};
    background: {ACCENT_SUBTLE};
    border: 1px solid {ACCENT};
    border-radius: {RADIUS_SM}px;
    padding: 2px 7px;
    font-size: {FONT_PT_UI_SM}pt;
}}
QFrame#workspace_card_divider {{ background: {BORDER_SUBTLE}; border: none; }}
QLabel#workspace_card_label {{ color: {FG_DIM}; }}
QLabel#workspace_project_chip {{
    color: {FG_SECONDARY};
    background: {BG_L2};
    border: none;
    border-radius: {RADIUS_SM}px;
    padding: 2px 7px;
    font-size: {FONT_PT_UI_SM}pt;
}}
QLabel#workspace_branch_link {{ color: {COLOR_LINK}; }}
QLabel#workspace_card_meta {{ color: {FG_DIM}; }}

/* ===== 聚合项目工作台 ===== */
QFrame#aggregate_project_sidebar {{
    background: {BG_L2};
    border: none;
    border-radius: {RADIUS_MD}px;
}}
QTreeWidget#aggregate_project_tree {{
    background: transparent;
    border: none;
}}
QTreeWidget#aggregate_project_tree::item {{
    min-height: {H_TABLE_ROW - GAP_XS}px;
    padding: {GAP_XS}px {GAP_SM}px;
}}
QTreeWidget#aggregate_project_tree::item:selected {{
    background: {ACCENT_SUBTLE};
}}
QFrame#aggregate_project_detail {{
    background: {BG_CONTENT};
    border: none;
}}
QFrame#service_header, QFrame#top_toolbar {{
    background: {BG_L0};
    border-bottom: 1px solid {BORDER_SUBTLE};
}}
QFrame#service_row:hover {{ background: {BG_L4}; }}
QScrollArea#project_cockpit {{ background: {BG_L1}; border: none; }}
QFrame#cockpit_overview {{
    background: {BG_CARD};
    border-radius: {RADIUS_LG}px;
}}
QFrame#cockpit_section {{
    background: {BG_L2};
    border-radius: {RADIUS_LG}px;
}}
QFrame#cockpit_runtime_row {{
    background: transparent;
    border-radius: {RADIUS_SM}px;
}}
QFrame#cockpit_runtime_row:hover {{ background: {BG_L4}; }}
QFrame#cockpit_event_row[clickable="true"] {{ border-radius: {RADIUS_SM}px; }}
QFrame#cockpit_event_row[clickable="true"]:hover {{ background: {BG_L3}; }}
QPushButton[runtimeName="true"] {{
    text-align: left;
    background: transparent;
    border-color: transparent;
}}
QPushButton[runtimeName="true"]:focus {{ border-color: {BORDER_FOCUS}; }}
QLabel[role="cockpit-title"] {{
    color: {FG_BRIGHT};
    font-size: {FONT_PT_UI_LG + 3}pt;
    font-weight: 600;
}}
QLabel[role="cockpit-chip"], QLabel[role="cockpit-event-ok"], QLabel[role="cockpit-event-error"] {{
    background: {BG_L1};
    border-radius: {RADIUS_SM}px;
    font-size: {FONT_PT_UI_SM}pt;
    padding: {GAP_XS}px {GAP_SM}px;
}}
QLabel[role="cockpit-chip"] {{ color: {FG_SECONDARY}; }}
QLabel[role="cockpit-event-ok"] {{ color: {COLOR_SUCCESS}; }}
QLabel[role="cockpit-event-error"] {{ color: {COLOR_ERROR}; }}
QScrollArea#service_rows_scroll {{
    background: {BG_CONTENT};
    border: none;
}}
QScrollArea#service_rows_scroll > QWidget > QWidget {{ background: {BG_CONTENT}; }}

QWidget#compact_toolbar {{
    background: {BG_L3};
    border-bottom: 1px solid {BORDER_SUBTLE};
}}

/* ===== 工具栏 ===== */
QToolBar {{
    background: {BG_L2};
    border-bottom: 1px solid {BORDER_SUBTLE};
    spacing: {GAP_XS}px;
    padding: {GAP_XS}px {GAP_SM}px;
}}

/* ===== Splitter（隐形 handle，hover 才高亮）===== */
QSplitter::handle {{
    background: {BORDER_SUBTLE};
}}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}
QSplitter::handle:hover {{ background: {ACCENT}; }}

/* ===== 滚动条（VSCode 风：默认低调，hover 显形）===== */
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {BG_L4};
    border-radius: 5px;
    min-height: 30px;
    margin: 2px;
}}
QScrollBar::handle:vertical:hover {{ background: {BG_L5}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}

QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: {BG_L4};
    border-radius: 5px;
    min-width: 30px;
    margin: 2px;
}}
QScrollBar::handle:horizontal:hover {{ background: {BG_L5}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: transparent; }}

/* ===== CheckBox ===== */
QCheckBox {{
    color: {FG_PRIMARY};
    spacing: 6px;
    background: transparent;
}}
QCheckBox::indicator {{
    width: 14px;
    height: 14px;
    border: 1px solid {BORDER_STRONG};
    border-radius: 3px;
    background: {BG_L3};
}}
QCheckBox::indicator:hover {{ border-color: {BORDER_FOCUS}; }}
QCheckBox::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
}}
QCheckBox:disabled {{ color: {FG_DIM}; }}

/* ===== GroupBox ===== */
QGroupBox {{
    background: transparent;
    color: {FG_PRIMARY};
    border: 1px solid {BORDER_SUBTLE};
    border-radius: {RADIUS_MD}px;
    margin-top: 12px;
    padding-top: 10px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: {FG_SECONDARY};
}}

/* ===== 表头 / 表格 ===== */
QHeaderView::section {{
    background: {BG_L2};
    color: {FG_SECONDARY};
    padding: 4px 8px;
    border: none;
    border-right: 1px solid {BORDER_SUBTLE};
    border-bottom: 1px solid {BORDER_SUBTLE};
}}
QHeaderView::section:hover {{ background: {BG_L4}; color: {FG_PRIMARY}; }}
QTableView, QTableWidget {{
    background: {BG_L1};
    color: {FG_PRIMARY};
    border: 1px solid {BORDER_SUBTLE};
    gridline-color: {BORDER_SUBTLE};
    selection-background-color: {ACCENT_SUBTLE};
    selection-color: {FG_BRIGHT};
}}
QTableView::item, QTableWidget::item {{ padding: 4px 6px; }}

/* ===== ToolTip ===== */
QToolTip {{
    background: {BG_L3};
    color: {FG_PRIMARY};
    border: 1px solid {BORDER_STRONG};
    padding: 4px 8px;
}}

/* ===== MessageBox ===== */
QMessageBox {{
    background: {BG_L2};
    color: {FG_PRIMARY};
}}
QMessageBox QLabel {{ color: {FG_PRIMARY}; }}
"""


def normalize_theme_name(name: str | None) -> str:
    return name if name in THEME_NAMES else DEFAULT_THEME


def current_theme() -> str:
    return _current_theme


def is_dark_theme() -> bool:
    return _current_theme == THEME_GITHUB_DARK


def _set_theme_colors(name: str) -> dict[str, str]:
    global _current_theme
    old_values = {
        key: str(value)
        for key, value in globals().items()
        if isinstance(value, ThemeColor)
    }
    palette = _THEME_PALETTES[name]
    for key, value in palette.items():
        token = globals().get(key)
        if isinstance(token, ThemeColor):
            token.set(value)
    _current_theme = name
    replacements: dict[str, str] = {}
    for key, value in palette.items():
        if key in old_values and old_values[key].lower() != value.lower():
            replacements.setdefault(old_values[key].lower(), value)
    return replacements


def _remap_existing_styles(app: QApplication, replacements: dict[str, str]) -> None:
    if not replacements:
        return
    pattern = re.compile(
        "|".join(re.escape(value) for value in sorted(replacements, key=len, reverse=True)),
        re.IGNORECASE,
    )
    for widget in app.allWidgets():
        style = widget.styleSheet()
        if style:
            widget.setStyleSheet(
                pattern.sub(lambda match: replacements[match.group(0).lower()], style)
            )


def _apply_palette(app: QApplication) -> None:
    palette = app.palette()
    palette.setColor(QPalette.ColorRole.Window, QColor(str(BG_L1)))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(str(FG_PRIMARY)))
    palette.setColor(QPalette.ColorRole.Base, QColor(str(BG_CODE)))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(str(BG_L2)))
    palette.setColor(QPalette.ColorRole.Text, QColor(str(FG_PRIMARY)))
    palette.setColor(QPalette.ColorRole.Button, QColor(str(BG_BTN)))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(str(FG_PRIMARY)))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(str(ACCENT)))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(str(FG_BRIGHT)))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(str(FG_DIM)))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(str(BG_L3)))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(str(FG_PRIMARY)))
    app.setPalette(palette)


def apply_theme(app: QApplication, name: str = DEFAULT_THEME) -> str:
    """主题入口：把完整 base QSS 套到 QApplication。

    完全自写 —— 不依赖 qdarkstyle / qt_material 等第三方主题。
    所有 widget 样式都来自上面 token 渲染的 QSS。
    """
    theme_name = normalize_theme_name(name)
    replacements = _set_theme_colors(theme_name)
    _remap_existing_styles(app, replacements)
    _apply_palette(app)
    app.setStyleSheet(_base_qss())
    return theme_name


# 兼容旧名（别的地方现在还在 import apply_dark_theme）
def apply_dark_theme(app: QApplication) -> None:
    apply_theme(app, THEME_GITHUB_DARK)


def apply_search_style(line_edit: QLineEdit) -> None:
    """把 QLineEdit 标记为搜索框样式：白字 + 等宽 + 加大字号 + 提亮 placeholder。

    所有可视为"搜索/查询"的输入框都应该过这一步：
    - 文件搜索 / 最近文件（quick_open）
    - 全项目内容搜索（content_search）
    - 文件树顶部过滤框（file_tree）
    - 日志搜索栏（log_widget）
    - 文件预览 Ctrl+F 搜索（file_preview）

    端口号、扩展名筛选这类"功能性输入"也走这套，保持视觉一致。
    """
    line_edit.setProperty("role", "search")
    pal = line_edit.palette()
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(str(FG_SECONDARY)))
    line_edit.setPalette(pal)
    # 注意：早期版本在这里调 style().unpolish/polish 想强制让 [role="search"]
    # 选择器立即生效，但在某些 widget（FilePreviewPane 构造期 search_input
    # 还没 attach 到父级时）会让 Qt 内部状态错乱，悄无声息地阻塞 widget
    # 后续显示——双击文件没反应、也没异常。
    # 实际上 setProperty 在 widget 第一次 polish 之前调用，Qt 会在 polish
    # 时自然吸收，不需要手动 repolish。已显示后再切换 role 才需要 repolish，
    # 但当前所有 caller 都是在 __init__ 里立刻调用，不存在那种情况。
