"""Markdown → PDF for the customer handover (the subset the project docs use).

Headings, paragraphs, bullet and numbered lists (one level of nesting), pipe tables, and
inline **bold**, `code` and [text](url). Arial throughout: it carries Cyrillic.

Arial has no emoji, and reportlab cannot draw a colour emoji font, so the card's marks
(❗️ ⚠️ ✅ ℹ️ 🔺 🔻 ➖ 💰) and the monitoring buttons' (⏸ ▶️) are drawn once from the system's
emoji font into small images and set inline — the guide explains the card by them, a box in
their place explains nothing.
"""

import re
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

EMOJI_FONT = "/System/Library/Fonts/Apple Color Emoji.ttc"
EMOJI_DIR = Path(tempfile.gettempdir()) / "handover-emoji"
# The monitoring buttons add «⏸» and «▶️» (U+23F8, U+25B6) — outside the blocks above;
# named one by one: the rest of U+25A0–25FF are plain shapes Arial draws itself.
_EMOJI = re.compile(
    "([\U0001f300-\U0001faff\u2600-\u27bf\u2139\u2b00-\u2bff\u23e9-\u23fa\u25b6\u25c0])\ufe0f?"
)


def _emoji_image(char: str) -> Path:
    """The emoji as a PNG on its own em square (160 px in this font: a thin «➖» keeps its
    room above and below); drawn once and kept next to the script."""
    from PIL import Image, ImageDraw, ImageFont

    target = EMOJI_DIR / f"{ord(char):x}.png"
    if not target.exists():
        EMOJI_DIR.mkdir(exist_ok=True)
        font = ImageFont.truetype(EMOJI_FONT, 160)
        left, top, right, bottom = font.getbbox(char)
        canvas = Image.new("RGBA", (right - left, bottom - top), (255, 255, 255, 0))
        ImageDraw.Draw(canvas).text((-left, -top), char, font=font, embedded_color=True)
        canvas.save(target)
    return target


def _emoji(match: re.Match) -> str:
    from PIL import Image

    path = _emoji_image(match.group(1))
    with Image.open(path) as image:
        width = 10 * image.width / image.height  # a tall «❗» stays tall, a flat «➖» flat
    return f'<img src="{path}" width="{width:.1f}" height="10" valign="-1.5"/>'


# Arial has no rouble sign; a font that has one draws just that glyph.
for _candidate in (
    "/System/Library/Fonts/SFNS.ttf",
    "/System/Library/Fonts/Supplemental/STIXTwoText.ttf",
):
    try:
        pdfmetrics.registerFont(TTFont("Rouble", _candidate))
        break
    except Exception:
        continue

FONTS = Path("/System/Library/Fonts/Supplemental")
pdfmetrics.registerFont(TTFont("Arial", str(FONTS / "Arial.ttf")))
pdfmetrics.registerFont(TTFont("Arial-Bold", str(FONTS / "Arial Bold.ttf")))
pdfmetrics.registerFont(TTFont("Arial-Italic", str(FONTS / "Arial Italic.ttf")))
pdfmetrics.registerFont(TTFont("Arial-BoldItalic", str(FONTS / "Arial Bold Italic.ttf")))
pdfmetrics.registerFont(TTFont("CourierNew", str(FONTS / "Courier New.ttf")))
pdfmetrics.registerFontFamily(
    "Arial", normal="Arial", bold="Arial-Bold", italic="Arial-Italic", boldItalic="Arial-BoldItalic"
)

INK = colors.HexColor("#1a1a1a")
MUTED = colors.HexColor("#555555")
RULE = colors.HexColor("#c8c8c8")
HEAD_FILL = colors.HexColor("#eef1f4")

BODY = ParagraphStyle(
    "body",
    fontName="Arial",
    fontSize=10,
    leading=14.5,
    textColor=INK,
    alignment=TA_LEFT,
    spaceAfter=6,
)
STYLES = {
    "h1": ParagraphStyle(
        "h1", parent=BODY, fontName="Arial-Bold", fontSize=19, leading=24, spaceAfter=10
    ),
    "h2": ParagraphStyle(
        "h2",
        parent=BODY,
        fontName="Arial-Bold",
        fontSize=14,
        leading=18,
        spaceBefore=14,
        spaceAfter=6,
        keepWithNext=True,
    ),
    "h3": ParagraphStyle(
        "h3",
        parent=BODY,
        fontName="Arial-Bold",
        fontSize=11.5,
        leading=15,
        spaceBefore=10,
        spaceAfter=4,
        keepWithNext=True,
    ),
    "li": ParagraphStyle("li", parent=BODY, leftIndent=16, bulletIndent=4, spaceAfter=3),
    "li2": ParagraphStyle("li2", parent=BODY, leftIndent=32, bulletIndent=20, spaceAfter=3),
    "cont": ParagraphStyle("cont", parent=BODY, leftIndent=16, spaceAfter=3),
    "cell": ParagraphStyle("cell", parent=BODY, fontSize=9, leading=12.5, spaceAfter=0),
    "head": ParagraphStyle(
        "head", parent=BODY, fontName="Arial-Bold", fontSize=9, leading=12.5, spaceAfter=0
    ),
    "note": ParagraphStyle("note", parent=BODY, fontSize=9, leading=12.5, textColor=MUTED),
}


def inline(text: str) -> str:
    """Markdown inline marks as reportlab's paragraph markup."""
    text = escape(text.strip())
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"`([^`]+)`", r'<font face="CourierNew" size="9.5">\1</font>', text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = text.replace("₽", '<font face="Rouble">₽</font>')
    return _EMOJI.sub(_emoji, text)


def table(lines: list[str], width: float) -> Table:
    rows = [[cell.strip() for cell in line.strip().strip("|").split("|")] for line in lines]
    rows = [row for row in rows if not all(re.fullmatch(r":?-{3,}:?", cell) for cell in row)]
    columns = len(rows[0])
    # Share the width by the longest cell of each column, within sane bounds.
    weights = [
        min(max(max(len(row[index]) for row in rows if index < len(row)), 8), 60)
        for index in range(columns)
    ]
    widths = [width * weight / sum(weights) for weight in weights]
    data = [
        [Paragraph(inline(cell), STYLES["head" if number == 0 else "cell"]) for cell in row]
        for number, row in enumerate(rows)
    ]
    built = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    built.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), HEAD_FILL),
                ("GRID", (0, 0), (-1, -1), 0.5, RULE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return built


def story(markdown: str, width: float) -> list:
    flow, lines, index = [], markdown.splitlines(), 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
        elif line.lstrip().startswith("|"):
            block = []
            while index < len(lines) and lines[index].lstrip().startswith("|"):
                block.append(lines[index])
                index += 1
            flow += [Spacer(1, 2), table(block, width), Spacer(1, 8)]
        elif match := re.match(r"(#{1,3})\s+(.*)", line):
            flow.append(Paragraph(inline(match.group(2)), STYLES[f"h{len(match.group(1))}"]))
            index += 1
        elif match := re.match(r"(\s*)(\d+)\.\s+(.*)", line):
            flow.append(
                Paragraph(inline(match.group(3)), STYLES["li"], bulletText=f"{match.group(2)}.")
            )
            index += 1
        elif match := re.match(r"(\s*)-\s+(.*)", line):
            style = "li2" if len(match.group(1)) >= 2 else "li"
            flow.append(Paragraph(inline(match.group(2)), STYLES[style], bulletText="•"))
            index += 1
        elif line.startswith("   "):  # a paragraph that continues a numbered item
            flow.append(Paragraph(inline(line), STYLES["cont"]))
            index += 1
        elif line.startswith("> "):
            flow.append(Paragraph(inline(line[2:]), STYLES["note"]))
            index += 1
        else:
            flow.append(Paragraph(inline(line), BODY))
            index += 1
    return flow


def build(source: Path, target: Path, title: str, footer: str) -> None:
    document = SimpleDocTemplate(
        str(target),
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=title,
        author="",
    )

    def page(canvas, doc) -> None:
        canvas.saveState()
        canvas.setFont("Arial", 8)
        canvas.setFillColor(MUTED)
        canvas.drawString(20 * mm, 10 * mm, footer)
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"стр. {doc.page}")
        canvas.restoreState()

    document.build(
        story(source.read_text(encoding="utf-8"), document.width),
        onFirstPage=page,
        onLaterPages=page,
    )


if __name__ == "__main__":
    build(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4])
