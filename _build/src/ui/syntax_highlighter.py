"""基于 Pygments 的 Qt 语法高亮器

用在文件预览窗口。支持 600+ 种语言（Java/Kotlin/Vue/JS/TS/Python/YAML/JSON/
SQL/HTML/CSS/XML/Groovy/Gradle/Properties...）。

实现思路：
- 一次 tokenize 整个文档，把 token 按 block_number 分桶
- highlightBlock 时只查表，O(1) 应用每个 token 的 format
- 文件大小 > MAX_HIGHLIGHT_BYTES 时自动退化为无高亮（避免卡住）
"""
from __future__ import annotations

import bisect
import logging
import time
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QThread, QTimer, Signal
from PySide6.QtGui import (
    QColor, QFont, QSyntaxHighlighter, QTextCharFormat, QTextDocument,
)

from src.core.search_worker_lifecycle import register_background_worker


perf_log = logging.getLogger("mini-ide.performance")

try:
    from pygments import lex
    from pygments.lexer import Lexer
    from pygments.lexers import get_lexer_by_name, get_lexer_for_filename
    from pygments.token import Token, STANDARD_TYPES
    from pygments.util import ClassNotFound
    _PYGMENTS_OK = True
except ImportError:
    _PYGMENTS_OK = False


MAX_HIGHLIGHT_BYTES = 100_000   # Pygments 持有 GIL，超过 100KB 不高亮


# ---- GitHub 代码高亮色板 ----

_COLORS = {
    "default":     "#abb2bf",
    "keyword":     "#c678dd",
    "keyword.declaration": "#c678dd",
    "keyword.namespace":   "#c678dd",
    "keyword.constant":    "#d19a66",
    "keyword.type":        "#e5c07b",
    "name":        "#abb2bf",
    "name.function": "#61afef",
    "name.function.magic": "#61afef",
    "name.class":  "#e5c07b",
    "name.builtin":"#e5c07b",
    "name.builtin.pseudo": "#e06c75",
    "name.decorator": "#61afef",
    "name.exception": "#e5c07b",
    "name.tag":    "#e06c75",
    "name.attribute": "#d19a66",
    "name.namespace": "#e5c07b",
    "name.constant": "#d19a66",
    "name.variable": "#e06c75",
    "name.variable.instance": "#e06c75",
    "name.label":  "#e06c75",
    "string":      "#98c379",
    "string.doc":  "#7f848e",
    "string.affix":"#98c379",
    "string.interpol": "#56b6c2",
    "string.escape": "#56b6c2",
    "string.regex":"#56b6c2",
    "string.symbol":"#98c379",
    "number":      "#d19a66",
    "operator":    "#56b6c2",
    "operator.word": "#c678dd",
    "punctuation": "#abb2bf",
    "comment":     "#5c6370",
    "comment.preproc": "#c678dd",
    "comment.special": "#c678dd",
    "generic.heading": "#61afef",
    "generic.subheading": "#61afef",
    "generic.deleted": "#e06c75",
    "generic.inserted": "#98c379",
    "generic.emph":"#abb2bf",
    "generic.strong":"#abb2bf",
    "literal":     "#d19a66",
    "error":       "#e06c75",
}

_LIGHT_COLORS = {
    "default": "#24292f", "keyword": "#cf222e", "keyword.declaration": "#cf222e",
    "keyword.namespace": "#cf222e", "keyword.constant": "#0550ae",
    "keyword.type": "#953800", "name": "#24292f", "name.function": "#8250df",
    "name.function.magic": "#8250df", "name.class": "#953800",
    "name.builtin": "#0550ae", "name.builtin.pseudo": "#0550ae",
    "name.decorator": "#8250df", "name.exception": "#953800",
    "name.tag": "#116329", "name.attribute": "#0550ae",
    "name.namespace": "#953800", "name.constant": "#0550ae",
    "name.variable": "#24292f", "name.variable.instance": "#24292f",
    "name.label": "#0550ae", "string": "#0a3069", "string.doc": "#6e7781",
    "string.affix": "#0a3069", "string.interpol": "#0a3069",
    "string.escape": "#0a3069", "string.regex": "#0a3069",
    "string.symbol": "#0a3069", "number": "#0550ae", "operator": "#cf222e",
    "operator.word": "#cf222e", "punctuation": "#24292f", "comment": "#6e7781",
    "comment.preproc": "#cf222e", "comment.special": "#8250df",
    "generic.heading": "#0550ae", "generic.subheading": "#0550ae",
    "generic.deleted": "#cf222e", "generic.inserted": "#1a7f37",
    "generic.emph": "#24292f", "generic.strong": "#24292f",
    "literal": "#0550ae", "error": "#cf222e",
}

_ITALIC_TOKENS = {"comment", "comment.preproc", "comment.special", "generic.emph", "string.doc"}
_BOLD_TOKENS = {"keyword", "name.class", "name.function", "generic.strong", "generic.heading"}


def _fmt_for(token_type) -> QTextCharFormat:
    from src.ui.theme import is_dark_theme

    colors = _COLORS if is_dark_theme() else _LIGHT_COLORS
    # token.Keyword 这种点分标识统一化成小写字符串
    key = str(token_type).lower()
    # pygments token name: "Token.Keyword.Declaration" -> "keyword.declaration"
    if key.startswith("token."):
        key = key[6:]

    color = None
    italic = False
    bold = False
    # 从最具体到最宽泛查找
    parts = key.split(".")
    while parts:
        k = ".".join(parts)
        if k in colors and color is None:
            color = colors[k]
        if k in _ITALIC_TOKENS:
            italic = True
        if k in _BOLD_TOKENS:
            bold = True
        parts.pop()
    if color is None:
        color = colors["default"]

    fmt = QTextCharFormat()
    fmt.setForeground(QColor(color))
    if italic:
        fmt.setFontItalic(True)
    if bold:
        fmt.setFontWeight(QFont.Weight.DemiBold)
    return fmt


_FORMAT_CACHE: dict[str, QTextCharFormat] = {}


def _get_format(token_type) -> QTextCharFormat:
    key = str(token_type)
    cached = _FORMAT_CACHE.get(key)
    if cached is None:
        cached = _fmt_for(token_type)
        _FORMAT_CACHE[key] = cached
    return cached


# ---- 文件名 → lexer ----

_LANG_BY_EXT = {
    ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".scala": "scala", ".groovy": "groovy",
    ".py": "python", ".pyw": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".jsx": "jsx",
    ".vue": "html+twig",   # pygments 没 vue lexer，用 html+embedded JS 近似；也可 fallback
    ".html": "html", ".htm": "html",
    ".css": "css", ".scss": "scss", ".less": "less",
    ".json": "json", ".jsonc": "json",
    ".xml": "xml", ".pom": "xml",
    ".yml": "yaml", ".yaml": "yaml",
    ".toml": "toml",
    ".properties": "properties", ".ini": "ini", ".cfg": "ini",
    ".sh": "bash", ".bash": "bash",
    ".bat": "batch", ".cmd": "batch", ".ps1": "powershell",
    ".sql": "sql",
    ".md": "markdown", ".markdown": "markdown",
    ".dockerfile": "docker",
    ".gradle": "groovy",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".cc": "cpp",
    ".go": "go", ".rs": "rust",
    ".rb": "ruby", ".php": "php",
    ".dart": "dart", ".swift": "swift",
    ".log": None,
}


def get_lexer_for(path: str, content: str = ""):
    """根据路径挑一个合适的 lexer。返回 None 表示不做高亮"""
    if not _PYGMENTS_OK:
        return None
    p = Path(path)
    name = p.name.lower()
    ext = p.suffix.lower()

    # 特殊文件名
    if name == "dockerfile":
        return _safe_lexer("docker")
    if name == "makefile":
        return _safe_lexer("makefile")
    if name.startswith("gradlew"):
        return None

    lang = _LANG_BY_EXT.get(ext)
    if lang is None and ext in _LANG_BY_EXT:
        return None   # 显式标记不高亮
    if lang:
        return _safe_lexer(lang)

    # 让 pygments 根据文件名猜
    try:
        return get_lexer_for_filename(name, stripnl=False)
    except Exception:
        # 基于内容猜（成本高，只在没有后缀时尝试）
        if content:
            try:
                from pygments.lexers import guess_lexer
                return guess_lexer(content[:4096], stripnl=False)
            except Exception:
                return None
        return None


def _safe_lexer(name: str):
    try:
        return get_lexer_by_name(name, stripnl=False)
    except ClassNotFound:
        return None


# ---- 后台分词 ----


def _build_block_token_index(text: str, lexer, should_stop) -> tuple[dict[int, list[tuple[int, int, str]]], int] | None:
    """在无 Qt 对象的线程中建立 block -> token 区间索引。"""
    block_starts: list[int] = [0]
    for pos, ch in enumerate(text):
        if ch == "\n":
            block_starts.append(pos + 1)

    formats: dict[int, list[tuple[int, int, str]]] = {}
    token_count = 0
    try:
        tokens = lexer.get_tokens_unprocessed(text)
        for start, token_type, token_text in tokens:
            token_count += 1
            if token_count % 128 == 0 and should_stop():
                return None
            if not token_text:
                continue
            token_key = str(token_type)
            segment_start = 0
            while segment_start < len(token_text):
                newline = token_text.find("\n", segment_start)
                segment_end = len(token_text) if newline < 0 else newline
                segment_len = segment_end - segment_start
                if segment_len:
                    absolute = start + segment_start
                    block_index = max(0, bisect.bisect_right(block_starts, absolute) - 1)
                    offset = absolute - block_starts[block_index]
                    formats.setdefault(block_index, []).append(
                        (offset, segment_len, token_key)
                    )
                if newline < 0:
                    break
                segment_start = newline + 1
    except Exception:
        return None
    return formats, token_count


class _TokenizeWorker(QThread):
    done = Signal(int, object, int, float)  # generation, index, token count, duration

    def __init__(self, generation: int, text: str, lexer, parent=None):
        super().__init__(parent)
        self.generation = generation
        self.text = text
        self.lexer = lexer

    def run(self) -> None:
        started = time.perf_counter()
        result = _build_block_token_index(
            self.text, self.lexer, self.isInterruptionRequested,
        )
        duration_ms = (time.perf_counter() - started) * 1000
        if result is None or self.isInterruptionRequested():
            perf_log.info(
                "perf op=syntax-tokenize duration_ms=%.1f files=0 status=stopped",
                duration_ms,
            )
            return
        index, token_count = result
        perf_log.info(
            "perf op=syntax-tokenize duration_ms=%.1f files=%d status=done",
            duration_ms, token_count,
        )
        self.done.emit(self.generation, index, token_count, duration_ms)


# ---- 高亮器 ----

class PygmentsHighlighter(QSyntaxHighlighter):
    """把 Pygments 的 token 流转成 Qt 的 block-level 高亮"""

    def __init__(self, document: QTextDocument, lexer=None):
        super().__init__(document)
        self._lexer = lexer
        # block_number -> list[(start_in_block, length, QTextCharFormat)]
        self._block_formats: dict[int, list[tuple[int, int, QTextCharFormat]]] = {}
        # 编辑后 200ms debounce 重新 tokenize：单次按键不立即触发，
        # 大文件不会高频卡顿；停止打字 200ms 颜色刷新（肉眼几乎不可察觉的延迟）。
        # 不挂这个的话编辑后旧 token 索引留着，反注释 `#` 后行还显示注释灰色。
        self._retokenize_timer = QTimer(self)
        self._retokenize_timer.setSingleShot(True)
        self._retokenize_timer.setInterval(200)
        self._retokenize_timer.timeout.connect(self.retokenize)
        document.contentsChange.connect(self._on_contents_change)
        self._generation = 0
        self._worker: _TokenizeWorker | None = None
        self._pending_request: tuple[int, str, object] | None = None
        self._applied_generation = 0
        self._last_tokenize_ms = 0.0
        self._last_apply_ms = 0.0
        if lexer:
            self.retokenize()

    def _on_contents_change(self, position: int, chars_removed: int, chars_added: int) -> None:
        if chars_removed == 0 and chars_added == 0:
            return   # contentsChange 偶发零变更触发，跳过
        self._generation += 1
        if self._worker and self._worker.isRunning():
            self._worker.requestInterruption()
        self._pending_request = None
        self._retokenize_timer.start()

    def set_lexer(self, lexer) -> None:
        self._lexer = lexer
        self.retokenize()

    def refresh_theme(self) -> None:
        _FORMAT_CACHE.clear()
        self.retokenize()

    def stop(self) -> None:
        """关闭预览时淘汰结果；线程由应用级生命周期统一等待。"""
        self._generation += 1
        self._pending_request = None
        if self._retokenize_timer.isActive():
            self._retokenize_timer.stop()
        if self._worker and self._worker.isRunning():
            self._worker.requestInterruption()

    def retokenize(self) -> None:
        """异步根据当前文档全文建立 block→formats 索引。"""
        self._generation += 1
        generation = self._generation
        if not self._lexer or not _PYGMENTS_OK:
            self._pending_request = None
            if self._worker and self._worker.isRunning():
                self._worker.requestInterruption()
            self._block_formats.clear()
            self.rehighlight()
            return
        text = self.document().toPlainText()
        if len(text.encode("utf-8", errors="ignore")) > MAX_HIGHLIGHT_BYTES:
            self._pending_request = None
            if self._worker and self._worker.isRunning():
                self._worker.requestInterruption()
            self._block_formats.clear()
            self.rehighlight()
            return

        request = (generation, text, self._lexer)
        if self._worker and self._worker.isRunning():
            self._pending_request = request
            self._worker.requestInterruption()
            return
        self._start_worker(*request)

    def _start_worker(self, generation: int, text: str, lexer) -> None:
        worker = _TokenizeWorker(
            generation, text, lexer, parent=QCoreApplication.instance(),
        )
        self._worker = worker
        worker.done.connect(self._apply_token_index)
        worker.finished.connect(lambda w=worker: self._on_worker_finished(w))
        worker.finished.connect(worker.deleteLater)
        register_background_worker(worker, worker.requestInterruption)
        worker.start()

    def _on_worker_finished(self, worker: _TokenizeWorker) -> None:
        if self._worker is worker:
            self._worker = None
        pending = self._pending_request
        self._pending_request = None
        if pending and pending[0] == self._generation and self._lexer:
            self._start_worker(*pending)

    def _apply_token_index(
        self, generation: int, raw_index: object, token_count: int, tokenize_ms: float,
    ) -> None:
        if generation != self._generation or not isinstance(raw_index, dict):
            return
        started = time.perf_counter()
        formats: dict[str, QTextCharFormat] = {}
        block_formats: dict[int, list[tuple[int, int, QTextCharFormat]]] = {}
        for block, entries in raw_index.items():
            converted = []
            for offset, length, token_key in entries:
                fmt = formats.get(token_key)
                if fmt is None:
                    fmt = formats[token_key] = _get_format(token_key)
                converted.append((offset, length, fmt))
            block_formats[block] = converted
        self._block_formats = block_formats
        self.rehighlight()
        duration_ms = (time.perf_counter() - started) * 1000
        self._applied_generation = generation
        self._last_tokenize_ms = tokenize_ms
        self._last_apply_ms = duration_ms
        perf_log.info(
            "perf op=syntax-highlight-apply duration_ms=%.1f files=%d status=done",
            duration_ms, token_count,
        )

    def highlightBlock(self, text: str) -> None:
        entries = self._block_formats.get(self.currentBlock().blockNumber())
        if not entries:
            return
        for offset, length, fmt in entries:
            self.setFormat(offset, length, fmt)
