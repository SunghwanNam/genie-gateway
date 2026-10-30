"""Genie Agent /items 응답을 다운로드 가능한 PDF 보고서로 변환한다."""

from __future__ import annotations

import json
from html import escape
from io import BytesIO
from typing import Any

def _items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    value = payload.get("items") or payload.get("data") or payload.get("output") or []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _text_blocks(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, str):
        if value.strip():
            found.append(value.strip())
    elif isinstance(value, list):
        for item in value:
            found.extend(_text_blocks(item))
    elif isinstance(value, dict):
        for key in ("text", "output_text", "content", "output"):
            if key in value:
                found.extend(_text_blocks(value[key]))
    return found


def _markdown_table(text: str) -> tuple[list[str], list[list[str]]] | None:
    lines = [line.strip() for line in text.splitlines() if line.strip().startswith("|")]
    if len(lines) < 2:
        return None
    rows = [[cell.strip() for cell in line.strip("|").split("|")] for line in lines]
    divider = all(cell.replace(":", "").replace("-", "").strip() == "" for cell in rows[1])
    if not divider:
        return None
    return rows[0], rows[2:]


def build_agent_report_pdf(payload: dict[str, Any], conversation_id: str) -> bytes:
    """서버가 재조회한 Agent items만 사용해 간결한 보고서를 만든다."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    pdfmetrics.registerFont(UnicodeCIDFont("HYSMyeongJo-Medium"))
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer, pagesize=A4, rightMargin=18 * mm, leftMargin=18 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm,
        title="Genie Agent 분석 보고서",
    )
    styles = getSampleStyleSheet()
    base = ParagraphStyle("Korean", parent=styles["BodyText"], fontName="HYSMyeongJo-Medium", fontSize=10, leading=16)
    title = ParagraphStyle("KoreanTitle", parent=base, fontSize=20, leading=28, alignment=TA_CENTER, spaceAfter=12)
    heading = ParagraphStyle("KoreanHeading", parent=base, fontSize=13, leading=20, spaceBefore=10, spaceAfter=5)
    story: list[Any] = [Paragraph("Genie Agent 분석 보고서", title), Paragraph(f"Conversation: {escape(conversation_id)}", base), Spacer(1, 8 * mm)]

    texts: list[str] = []
    code_blocks: list[str] = []
    for item in _items(payload):
        item_type = str(item.get("type", ""))
        if item_type == "function_call":
            args = item.get("arguments") or item.get("args")
            if args:
                code_blocks.append(args if isinstance(args, str) else json.dumps(args, ensure_ascii=False, indent=2))
        else:
            texts.extend(_text_blocks(item))

    story.append(Paragraph("분석 결과", heading))
    if not texts:
        story.append(Paragraph("표시할 최종 응답이 없습니다.", base))
    for text in dict.fromkeys(texts):
        table = _markdown_table(text)
        if table:
            headers, rows = table
            data = [[Paragraph(escape(cell), base) for cell in headers]] + [
                [Paragraph(escape(cell), base) for cell in row] for row in rows
            ]
            grid = Table(data, repeatRows=1, hAlign="LEFT")
            grid.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEF1F4")),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CDD5DF")),
                ("FONTNAME", (0, 0), (-1, -1), "HYSMyeongJo-Medium"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ]))
            story.extend([grid, Spacer(1, 5 * mm)])
        else:
            story.extend([Paragraph(escape(text).replace("\n", "<br/>"), base), Spacer(1, 4 * mm)])

    if code_blocks:
        story.append(Paragraph("코드 및 실행 정보", heading))
        for code in code_blocks:
            story.append(Paragraph(escape(code).replace("\n", "<br/>"), base))
            story.append(Spacer(1, 3 * mm))

    document.build(story)
    return buffer.getvalue()
