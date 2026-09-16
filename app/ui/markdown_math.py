from __future__ import annotations

import html
import re
from dataclasses import dataclass

from PySide6.QtCore import QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QMouseEvent, QTextCursor, QTextDocument
from PySide6.QtWidgets import QSizePolicy, QTextEdit, QWidget

from app.ui.citation_widgets import activate_source_group, insert_inline_citations


_CODE_PATTERN = re.compile(r"```[\s\S]*?```|`[^`\n]*`")
_MATH_PROTECTED_PATTERN = re.compile(
    r"!?\[[^\]\n]*\]\([^\)\n]*\)"
    r"|\[\s*P\d+\s*[·.:]\s*p\.?\s*\d+\s*\]"
    r"|<[^>\n]+>"
    r"|https?://[^\s<]+",
    re.IGNORECASE,
)
_MATH_PATTERN = re.compile(
    r"\$\$([\s\S]*?)\$\$|\\\[([\s\S]*?)\\\]|\\\((.*?)\\\)|(?<!\\)\$(?!\$)([^$\n]+?)(?<!\\)\$"
)
_RAW_MATH_PATTERN = re.compile(
    r"(?<![\\\w])(?:"
    r"\\(?:hat|bar|vec|tilde|dot|ddot)\s*\{[^{}\n]{1,64}\}"
    r"(?:_(?:\{[^{}\n]{1,32}\}|[A-Za-z0-9]+))?"
    r"(?:\^(?:\{[^{}\n]{1,32}\}|[A-Za-z0-9+\-=]+))?"
    r"|\\(?:sum|prod|int|alpha|beta|gamma|delta|epsilon|varepsilon|theta|lambda|mu|pi|rho|sigma|tau|phi|omega|odot)"
    r"(?:_(?:\{[^{}\n]{1,32}\}|[A-Za-z0-9]+))?"
    r"(?:\^(?:\{[^{}\n]{1,32}\}|[A-Za-z0-9+\-=]+))?"
    r"|\\sqrt\s*\{(?:[^{}\n]|\{[^{}\n]*\}){1,96}\}"
    r"|\\frac\s*\{(?:[^{}\n]|\{[^{}\n]*\}){1,96}\}"
    r"\s*\{(?:[^{}\n]|\{[^{}\n]*\}){1,96}\}"
    r"|[A-Za-z](?:_(?:\{[A-Za-z0-9+\-=, ]{1,32}\}|[A-Za-z0-9]+)"
    r"|\^(?:\{[A-Za-z0-9+\-=, ]{1,32}\}|[A-Za-z0-9+\-=]+))"
    r"(?:_(?:\{[A-Za-z0-9+\-=, ]{1,32}\}|[A-Za-z0-9]+)"
    r"|\^(?:\{[A-Za-z0-9+\-=, ]{1,32}\}|[A-Za-z0-9+\-=]+))?"
    r"(?:\([^()\n]{1,40}\))?"
    r")(?![A-Za-z0-9_@]|\.[A-Za-z0-9])"
)
_MATRIX_PATTERN = re.compile(
    r"\\begin\{(bmatrix|pmatrix|matrix)\}([\s\S]*?)\\end\{\1\}"
)
_SCRIPT_PATTERN = re.compile(
    r"(RAHTMLTOKEN\d+Z|\\[A-Za-z]+|[A-Za-z0-9\)\]])"
    r"(?:_\{([^{}]+)\}|_([A-Za-z0-9]+))?"
    r"(?:\^\{([^{}]+)\}|\^([A-Za-z0-9+\-=]+))?"
)

_COMMANDS = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ",
    "epsilon": "ε", "varepsilon": "ε", "theta": "θ", "lambda": "λ", "mu": "μ",
    "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ", "phi": "φ",
    "omega": "ω", "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ",
    "Lambda": "Λ", "Sigma": "Σ", "Phi": "Φ", "Omega": "Ω",
    "sum": "∑", "prod": "∏", "int": "∫", "times": "×", "cdot": "·",
    "odot": "⊙",
    "pm": "±", "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥",
    "neq": "≠", "approx": "≈", "infty": "∞", "to": "→",
}

_ACCENTS = {
    "hat": "\u0302",
    "bar": "\u0304",
    "vec": "\u20d7",
    "tilde": "\u0303",
    "dot": "\u0307",
    "ddot": "\u0308",
}


@dataclass(frozen=True)
class _MathToken:
    placeholder: str
    expression: str
    block: bool


def _math_tokens(markdown: str) -> tuple[str, list[_MathToken]]:
    tokens: list[_MathToken] = []

    def parse_segment(segment: str) -> str:
        def token(expression: str, *, block: bool = False) -> str:
            # Alphanumeric-only placeholders cannot acquire Markdown emphasis,
            # code, or link formatting from punctuation around a formula.
            placeholder = f"RAMATHTOKEN{len(tokens):04d}Z"
            tokens.append(_MathToken(placeholder, expression.strip(), block))
            return f"\n\n{placeholder}\n\n" if block else placeholder

        def replace(match: re.Match[str]) -> str:
            expression = next(value for value in match.groups() if value is not None)
            block = match.group(1) is not None or match.group(2) is not None
            return token(expression, block=block)

        def parse_text(value: str) -> str:
            delimited = _MATH_PATTERN.sub(replace, value)
            return _RAW_MATH_PATTERN.sub(
                lambda match: token(match.group(0)),
                delimited,
            )

        output: list[str] = []
        position = 0
        for protected in _MATH_PROTECTED_PATTERN.finditer(segment):
            output.append(parse_text(segment[position : protected.start()]))
            output.append(protected.group(0))
            position = protected.end()
        output.append(parse_text(segment[position:]))
        return "".join(output)

    output: list[str] = []
    position = 0
    for match in _CODE_PATTERN.finditer(markdown):
        output.append(parse_segment(markdown[position : match.start()]))
        output.append(match.group(0))
        position = match.end()
    output.append(parse_segment(markdown[position:]))
    return "".join(output), tokens


def _latex_html(expression: str) -> str:
    raw = expression.strip()
    fragments: dict[str, str] = {}

    def stash(value: str) -> str:
        token = f"RAHTMLTOKEN{len(fragments)}Z"
        fragments[token] = value
        return token

    def matrix(match: re.Match[str]) -> str:
        kind, body = match.groups()
        rows = [row.strip() for row in re.split(r"\\\\", body) if row.strip()]
        cells = [
            [f"<td>{_latex_html(cell.strip())}</td>" for cell in row.split("&")]
            for row in rows
        ]
        table = "<table cellspacing='4' cellpadding='1'>" + "".join(
            "<tr>" + "".join(row) + "</tr>" for row in cells
        ) + "</table>"
        brackets = ("(", ")") if kind == "pmatrix" else ("[", "]")
        return stash(f"{brackets[0]}{table}{brackets[1]}")

    raw = _MATRIX_PATTERN.sub(matrix, raw)

    accent = re.compile(
        r"\\(hat|bar|vec|tilde|dot|ddot)\s*\{([^{}]+)\}"
    )
    while accent.search(raw):
        raw = accent.sub(
            lambda match: stash(
                f"<span>{_latex_html(match.group(2))}{_ACCENTS[match.group(1)]}</span>"
            ),
            raw,
        )

    nested_content = r"((?:[^{}]|\{[^{}]*\})+)"
    fraction = re.compile(
        rf"\\frac\s*\{{{nested_content}\}}\s*\{{{nested_content}\}}"
    )
    while fraction.search(raw):
        raw = fraction.sub(
            lambda match: stash(
                f"<span><sup>{_latex_html(match.group(1))}</sup>⁄"
                f"<sub>{_latex_html(match.group(2))}</sub></span>"
            ),
            raw,
        )
    raw = re.sub(
        rf"\\sqrt\s*\{{{nested_content}\}}",
        lambda match: stash(f"√({ _latex_html(match.group(1)) })"),
        raw,
    )

    def script(match: re.Match[str]) -> str:
        base, sub_braced, sub_plain, sup_braced, sup_plain = match.groups()
        if not any((sub_braced, sub_plain, sup_braced, sup_plain)):
            return match.group(0)
        base_html = fragments[base] if base in fragments else _latex_html(base)
        sub = sub_braced or sub_plain
        sup = sup_braced or sup_plain
        value = base_html
        if sub:
            value += f"<sub>{_latex_html(sub)}</sub>"
        if sup:
            value += f"<sup>{_latex_html(sup)}</sup>"
        return stash(value)

    raw = _SCRIPT_PATTERN.sub(script, raw)
    escaped = html.escape(raw)
    for command, symbol in _COMMANDS.items():
        escaped = re.sub(rf"\\{command}(?![A-Za-z])", symbol, escaped)
    escaped = escaped.replace(r"\,", " ").replace(r"\;", " ")
    escaped = escaped.replace(r"\left", "").replace(r"\right", "")
    escaped = escaped.replace("{", "").replace("}", "")
    for token, fragment in fragments.items():
        escaped = escaped.replace(token, fragment)
    return escaped


def _streaming_safe_markdown(markdown: str) -> str:
    """Hide only an unfinished trailing formula until its next stream chunk."""
    tail_start = max(0, len(markdown) - 240)
    tail = markdown[tail_start:]
    candidates: list[int] = []

    for opener, closer in ((r"\(", r"\)"), (r"\[", r"\]"), ("$$", "$$")):
        position = tail.rfind(opener)
        if position >= 0 and tail.find(closer, position + len(opener)) < 0:
            candidates.append(tail_start + position)

    single_dollars = [
        match.start()
        for match in re.finditer(r"(?<!\\)(?<!\$)\$(?!\$)", tail)
    ]
    if len(single_dollars) % 2:
        position = single_dollars[-1]
        unfinished = tail[position + 1 :]
        if re.search(r"[\\_^=]|[A-Za-z]\s*[+\-*/]", unfinished):
            candidates.append(tail_start + position)

    unfinished_command = re.search(
        r"\\(?:hat|bar|vec|tilde|dot|ddot|sqrt|frac)"
        r"(?:\{[^}\n]*)?$",
        tail,
    )
    if unfinished_command:
        candidates.append(tail_start + unfinished_command.start())

    return markdown[: min(candidates)] if candidates else markdown


def set_markdown_math(
    document: QTextDocument,
    markdown: str,
    *,
    streaming: bool = False,
) -> None:
    source = str(markdown or "")
    if streaming:
        source = _streaming_safe_markdown(source)
    prepared, tokens = _math_tokens(source)
    document.setMarkdown(prepared)
    for token in tokens:
        cursor = document.find(token.placeholder)
        if cursor.isNull():
            continue
        rendered = _latex_html(token.expression)
        if token.block:
            rendered = (
                "<div align='center' style='margin:6px 0'>"
                f"{rendered}</div>"
            )
        else:
            rendered = f"<span style='font-style:italic'>{rendered}</span>"
        cursor.insertHtml(rendered)


class AutoExpandingMarkdownEdit(QTextEdit):
    """Editable rendered Markdown/math field with one outer-scroll-first height."""

    citation_requested = Signal(object)

    def __init__(
        self,
        text: str = "",
        parent: QWidget | None = None,
        *,
        minimum_height: int = 66,
        maximum_height: int = 320,
    ) -> None:
        super().__init__(parent)
        self._source_text = ""
        self._minimum_content_height = minimum_height
        self._maximum_content_height = maximum_height
        self._citation_groups: dict[str, list[object]] = {}
        self.setAcceptRichText(False)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.document().setDocumentMargin(6)
        self.document().documentLayout().documentSizeChanged.connect(
            lambda _size: self._schedule_height()
        )
        self.textChanged.connect(self._schedule_height)
        self.setPlainText(text)

    def setPlainText(self, text: str) -> None:
        self._source_text = str(text or "")
        set_markdown_math(self.document(), self._source_text)
        self.document().setModified(False)
        self._citation_groups.clear()
        self._schedule_height()

    def toPlainText(self) -> str:
        if not self.document().isModified():
            return self._source_text
        return super().toPlainText().replace("\ufffc", "").strip()

    def set_citations(self, citations: object, numbering: object = None) -> None:
        self._citation_groups = insert_inline_citations(
            self.document(), citations, numbering
        )
        self.document().setModified(False)
        self._schedule_height()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        anchor = self.anchorAt(event.position().toPoint())
        url = QUrl(anchor) if anchor else QUrl()
        if url.scheme() == "ra-source":
            group = self._citation_groups.get(url.host())
            if group:
                activate_source_group(self, group, self.citation_requested.emit)
                event.accept()
                return
        super().mousePressEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.document().setTextWidth(max(40, self.viewport().width()))
        self._schedule_height()

    def sizeHint(self) -> QSize:
        return QSize(super().sizeHint().width(), self.height())

    def _schedule_height(self, *_args: object) -> None:
        QTimer.singleShot(0, self._adjust_height)

    def _adjust_height(self) -> None:
        self.document().setTextWidth(max(40, self.viewport().width()))
        desired = int(self.document().size().height()) + 8
        height = max(
            self._minimum_content_height,
            min(self._maximum_content_height, desired),
        )
        self.setFixedHeight(height)
        self.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
            if desired > self._maximum_content_height
            else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
