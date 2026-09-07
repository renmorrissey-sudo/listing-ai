"""Server-generated PDF rendering for saved CMA reports."""

from __future__ import annotations

from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


NAVY = colors.HexColor("#172554")
BLUE = colors.HexColor("#1D4ED8")
SLATE = colors.HexColor("#52637A")
LIGHT_BLUE = colors.HexColor("#EFF6FF")
BORDER = colors.HexColor("#D1D5DB")


def _money(value):
    return f"${float(value or 0):,.0f}"


def _number(value):
    return f"{int(value or 0):,}"


def _safe(value):
    return escape(str(value or ""))


def _footer(canvas, document):
    canvas.saveState()
    canvas.setStrokeColor(BORDER)
    canvas.line(document.leftMargin, 0.48 * inch, letter[0] - document.rightMargin, 0.48 * inch)
    canvas.setFillColor(SLATE)
    canvas.setFont("Helvetica", 8)
    canvas.drawString(document.leftMargin, 0.3 * inch, "TopAI Real Estate Tools - Comparative Market Analysis")
    canvas.drawRightString(letter[0] - document.rightMargin, 0.3 * inch, f"Page {document.page}")
    canvas.restoreState()


def render_cma_pdf(report):
    """Return a printable CMA report as PDF bytes."""
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=0.45 * inch,
        leftMargin=0.45 * inch,
        topMargin=0.45 * inch,
        bottomMargin=0.62 * inch,
        title=f"CMA - {report.get('subject_address') or 'Property'}",
        author="TopAI Real Estate Tools",
    )
    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "CmaTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=22,
        leading=26,
        textColor=colors.white,
        alignment=TA_CENTER,
        spaceAfter=8,
    )
    subtitle = ParagraphStyle(
        "CmaSubtitle",
        parent=styles["Normal"],
        fontSize=11,
        leading=15,
        textColor=colors.white,
        alignment=TA_CENTER,
    )
    heading = ParagraphStyle(
        "CmaHeading",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=13,
        leading=16,
        textColor=NAVY,
        spaceBefore=10,
        spaceAfter=7,
    )
    body = ParagraphStyle(
        "CmaBody",
        parent=styles["BodyText"],
        fontSize=8.5,
        leading=12,
        textColor=SLATE,
    )
    small = ParagraphStyle(
        "CmaSmall",
        parent=body,
        fontSize=7.2,
        leading=9,
    )

    criteria = report.get("criteria") or {}
    analysis = report.get("analysis") or {}
    cover = Table(
        [[
            Paragraph(
                f"COMPETITIVE MARKET ANALYSIS<br/><br/><font size='16'>{_safe(report.get('subject_address'))}</font>",
                title,
            ),
            Paragraph(
                f"{_safe(report.get('city'))}, {_safe(report.get('state'))}<br/>"
                f"Prepared {_safe(criteria.get('report_date'))}<br/>"
                f"{_safe(criteria.get('prospect_type_label'))} prospect",
                subtitle,
            ),
        ]],
        colWidths=[4.6 * inch, 2.15 * inch],
    )
    cover.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), NAVY),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 18),
        ("RIGHTPADDING", (0, 0), (-1, -1), 18),
        ("TOPPADDING", (0, 0), (-1, -1), 20),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 20),
    ]))

    story = [cover, Spacer(1, 12), Paragraph("Pricing overview", heading)]
    overview = [
        ["Automated market estimate", "Indicated range", "Median comp price", "Average price / sq. ft."],
        [
            _money(analysis.get("indicated_value")),
            f"{_money(analysis.get('indicated_range_low'))} - {_money(analysis.get('indicated_range_high'))}",
            _money(analysis.get("median_sale_price")),
            _money(analysis.get("average_price_per_sqft")),
        ],
    ]
    overview_table = Table(overview, colWidths=[1.7 * inch] * 4)
    overview_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), LIGHT_BLUE),
        ("TEXTCOLOR", (0, 0), (-1, 0), SLATE),
        ("TEXTCOLOR", (0, 1), (-1, 1), NAVY),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 7),
        ("FONTSIZE", (0, 1), (-1, 1), 11),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.5, BORDER),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.extend([overview_table, Paragraph("Target property", heading)])
    subject_rows = [
        ["Property type", "Bedrooms", "Bathrooms", "Square feet", "Lookback"],
        [
            criteria.get("property_type_label") or "",
            str(criteria.get("beds") or ""),
            str(criteria.get("baths") or ""),
            _number(criteria.get("sqft")),
            f"{criteria.get('date_range_months') or ''} months",
        ],
    ]
    subject_table = Table(subject_rows, colWidths=[1.55 * inch, 1.05 * inch, 1.05 * inch, 1.4 * inch, 1.4 * inch])
    subject_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F8FAFC")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("TEXTCOLOR", (0, 0), (-1, 0), SLATE),
        ("TEXTCOLOR", (0, 1), (-1, 1), NAVY),
        ("GRID", (0, 0), (-1, -1), 0.5, BORDER),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))

    story.extend([subject_table, Paragraph("Selected comparable market evidence", heading)])
    comp_rows = [["Property", "Reference", "Price", "Beds / Baths", "Sq. ft.", "Distance", "Match"]]
    for comp in report.get("selected_comparables") or []:
        address = Paragraph(
            f"<b>{_safe(comp.get('address'))}</b><br/><font color='#52637A'>{_safe(comp.get('verification_source'))}</font>",
            small,
        )
        distance = "--" if comp.get("distance_miles") is None else f"{comp['distance_miles']:.2f} mi"
        comp_rows.append([
            address,
            str(comp.get("sale_date") or ""),
            _money(comp.get("sale_price")),
            f"{comp.get('beds')} / {comp.get('baths')}",
            _number(comp.get("sqft")),
            distance,
            f"{float(comp.get('similarity_score') or 0):.1f}",
        ])
    comp_table = Table(
        comp_rows,
        repeatRows=1,
        colWidths=[2.25 * inch, 0.72 * inch, 0.82 * inch, 0.72 * inch, 0.6 * inch, 0.62 * inch, 0.48 * inch],
    )
    comp_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), BLUE),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 7),
        ("FONTSIZE", (0, 1), (-1, -1), 7),
        ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 1), (-1, -1), "CENTER"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(comp_table)

    methodology = (
        "The indicated value and range come from the RentCast automated valuation model, "
        "which evaluates the exact location, subject attributes, nearby market evidence, "
        "distance, and comparable correlation."
        if analysis.get("valuation_method") == "provider_avm"
        else
        "The indication applies selected comparable price per square foot to the target size "
        "and weights the results by property similarity."
    )
    disclosure = (
        "This comparative market analysis is not an appraisal or guarantee of market value. "
        "AVM comparable prices and reference dates may reflect active or inactive sale listings "
        "rather than verified closed-sale amounts. Verify status, final sale price, concessions, "
        "condition, geographic boundaries, and material facts in the local MLS and public records."
    )
    story.extend([
        KeepTogether([Paragraph("Methodology", heading), Paragraph(methodology, body)]),
        KeepTogether([Paragraph("Important disclosure", heading), Paragraph(disclosure, body)]),
    ])
    document.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buffer.getvalue()
