"""Generate a RoboCall AI PDF report.

Two reports share this layout; each one's prose lives in its own module.

  python docs/build_report.py            -> docs/RoboCall-AI-Project-Report.pdf
  python docs/build_report.py carrier    -> docs/RoboCall-AI-Status-Report.pdf

`current` is the live one: what the project is, where it stands, what works.
`carrier` is the 14 September evaluation that chose Telnyx over Twilio, kept
because it is the record of why that decision was made.
"""
from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate, Frame, HRFlowable, Image, KeepTogether, PageBreak,
    PageTemplate, Paragraph, Spacer, Table, TableStyle,
)

HERE = Path(__file__).resolve().parent
SHOT = HERE / "call-log.png"

# name -> (content module, output file, page title, footer, PDF subject)
REPORTS = {
    "current": (
        "story_current",
        "RoboCall-AI-Project-Report.pdf",
        "RoboCall AI - Project Report",
        "RoboCall AI — Project Report",
        "What the project is, how it is built, and where it stands",
    ),
    "brief": (
        "story_brief",
        "RoboCall-AI-Requirements-Check.pdf",
        "RoboCall AI - Requirements Check",
        "RoboCall AI — Requirements Check",
        "Every item in the brief, and where it lives in the product",
    ),
    "guide": (
        "story_guide",
        "RoboCall-AI-User-Guide.pdf",
        "RoboCall AI - User Guide",
        "RoboCall AI — User Guide",
        "Setting it up, sending a campaign, reading the results",
    ),
    "compliance": (
        "story_compliance",
        "RoboCall-AI-Compliance-Briefing.pdf",
        "RoboCall AI - Telemarketing Compliance Briefing",
        "RoboCall AI — Telemarketing Compliance",
        "US telemarketing obligations and what the software does about them",
    ),
    "carrier": (
        "story",
        "RoboCall-AI-Status-Report.pdf",
        "RoboCall AI - Status and Recommendation",
        "RoboCall AI — Status and Recommendation",
        "Phase 1 build status, carrier evaluation",
    ),
}

_FOOTER = REPORTS["current"][3]

INK = colors.HexColor("#1a1d21")
MUTED = colors.HexColor("#5b6571")
RULE = colors.HexColor("#d7dce2")
ACCENT = colors.HexColor("#1f6feb")
BAD = colors.HexColor("#b4232a")
GOOD = colors.HexColor("#1a7f4b")
PANEL = colors.HexColor("#f4f6f8")

MARGIN = 0.85 * inch
PAGE_W, PAGE_H = LETTER
BODY_W = PAGE_W - 2 * MARGIN

# --- styles -----------------------------------------------------------------

ss = getSampleStyleSheet()


def style(name, **kw):
    base = kw.pop("parent", ss["Normal"])
    return ParagraphStyle(name, parent=base, **kw)


S = {
    "title": style("title", fontName="Helvetica-Bold", fontSize=23, leading=27,
                   textColor=INK, spaceAfter=4),
    "subtitle": style("subtitle", fontName="Helvetica", fontSize=11.5, leading=15,
                      textColor=MUTED, spaceAfter=2),
    "meta": style("meta", fontName="Helvetica", fontSize=8.5, leading=12,
                  textColor=MUTED),
    "h1": style("h1", fontName="Helvetica-Bold", fontSize=15, leading=19,
                textColor=INK, spaceBefore=18, spaceAfter=7),
    "h2": style("h2", fontName="Helvetica-Bold", fontSize=11.5, leading=15,
                textColor=INK, spaceBefore=12, spaceAfter=4),
    "body": style("body", fontName="Helvetica", fontSize=9.8, leading=14.2,
                  textColor=INK, spaceAfter=7, alignment=TA_LEFT),
    "bullet": style("bullet", fontName="Helvetica", fontSize=9.8, leading=14.2,
                    textColor=INK, spaceAfter=3, leftIndent=13, bulletIndent=3),
    "caption": style("caption", fontName="Helvetica-Oblique", fontSize=8.8,
                     leading=12.6, textColor=MUTED, spaceBefore=6, spaceAfter=4),
    "code": style("code", fontName="Courier", fontSize=8.2, leading=11.6,
                  textColor=INK),
    "cell": style("cell", fontName="Helvetica", fontSize=8.8, leading=12),
    "cellb": style("cellb", fontName="Helvetica-Bold", fontSize=8.8, leading=12),
    "note": style("note", fontName="Helvetica", fontSize=9.2, leading=13.4,
                  textColor=INK, spaceAfter=4),
}


def P(text, s="body"):
    return Paragraph(text, S[s])


def bullets(items, s="bullet"):
    return [Paragraph(t, S[s], bulletText="•") for t in items]


def rule(space_before=2, space_after=8):
    return HRFlowable(width="100%", thickness=0.6, color=RULE,
                      spaceBefore=space_before, spaceAfter=space_after)


def callout(text, border=ACCENT, bg=PANEL):
    """A single-cell shaded panel with a coloured left edge."""
    t = Table([[Paragraph(text, S["note"])]], colWidths=[BODY_W])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("LINEBEFORE", (0, 0), (0, -1), 2.5, border),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return t


def code_block(lines):
    body = "<br/>".join(lines)
    t = Table([[Paragraph(body, S["code"])]], colWidths=[BODY_W])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f0f2f5")),
        ("BOX", (0, 0), (-1, -1), 0.5, RULE),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    return t


def table(rows, widths, header=True, aligns=None):
    data = []
    for r_i, row in enumerate(rows):
        line = []
        for c_i, cell in enumerate(row):
            if isinstance(cell, Paragraph):
                line.append(cell)
            else:
                st = "cellb" if (header and r_i == 0) else "cell"
                line.append(Paragraph(str(cell), S[st]))
        data.append(line)

    t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    cmds = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 5.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5.5),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, RULE),
    ]
    if header:
        cmds += [
            ("BACKGROUND", (0, 0), (-1, 0), PANEL),
            ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.HexColor("#b9c1cb")),
        ]
    for col, al in (aligns or {}).items():
        cmds.append(("ALIGN", (col, 0), (col, -1), al))
    t.setStyle(TableStyle(cmds))
    return t


# --- page furniture ---------------------------------------------------------

def decorate(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7.6)
    canvas.setFillColor(MUTED)
    canvas.drawString(MARGIN, 0.52 * inch, _FOOTER)
    canvas.drawRightString(PAGE_W - MARGIN, 0.52 * inch, f"Page {doc.page}")
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.5)
    canvas.line(MARGIN, 0.70 * inch, PAGE_W - MARGIN, 0.70 * inch)
    canvas.restoreState()


def build(module_name: str, out: Path, title: str, subject: str):
    doc = BaseDocTemplate(
        str(out), pagesize=LETTER,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=0.95 * inch,
        title=title, author="Engineering", subject=subject,
    )
    frame = Frame(MARGIN, 0.95 * inch, BODY_W, PAGE_H - MARGIN - 0.95 * inch,
                  id="body", leftPadding=0, rightPadding=0,
                  topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=decorate)])
    doc.build(story(module_name))


# --- content ----------------------------------------------------------------

def story(module_name: str):
    module = __import__(module_name)
    return module.build_story({
        "P": P, "table": table, "callout": callout, "code_block": code_block,
        "rule": rule, "bullets": bullets,
        "BODY_W": BODY_W, "SHOT": SHOT,
        "BAD": BAD, "GOOD": GOOD, "ACCENT": ACCENT, "MUTED": MUTED,
    })


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(HERE))
    which = sys.argv[1] if len(sys.argv) > 1 else "current"
    if which not in REPORTS:
        raise SystemExit(f"unknown report {which!r}; choose from {', '.join(REPORTS)}")

    module_name, filename, title, footer, subject = REPORTS[which]
    _FOOTER = footer
    out = HERE / filename
    build(module_name, out, title, subject)
    print(f"wrote {out} ({out.stat().st_size:,} bytes)")
