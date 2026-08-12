"""Parse and safely render a saved Listing Generator prospect email."""

from __future__ import annotations

import html
import re

_SUBJECT_RE = re.compile(
    r"^\s*(?:\*{0,2})subject(?:\s+line)?(?:\*{0,2})\s*:\s*(.+?)\s*$",
    re.IGNORECASE,
)
_PARAGRAPH_RE = re.compile(r"\n\s*\n+")


def parse_listing_email(raw: str | None, property_address: str) -> dict:
    """Return subject/body from the exact saved generated email string."""
    text = (raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise ValueError("This listing does not contain generated email content.")

    lines = text.split("\n")
    subject = None
    subject_index = None
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        match = _SUBJECT_RE.match(line)
        if match:
            subject = match.group(1).strip().strip("*").strip()
            subject_index = index
        break

    if subject_index is not None:
        body = "\n".join(lines[subject_index + 1 :]).strip()
    else:
        body = text
    if not subject:
        subject = f"New listing: {(property_address or 'Property').strip()}"
    if not body:
        body = text
    return {"subject": subject[:998], "body": body}


def render_listing_email_html(
    *,
    subject: str,
    body: str,
    property_address: str,
) -> str:
    """Create a professional HTML draft while escaping all generated input."""
    safe_subject = html.escape(subject or "", quote=True)
    safe_address = html.escape(property_address or "", quote=True)
    paragraphs = []
    for paragraph in _PARAGRAPH_RE.split((body or "").strip()):
        if not paragraph.strip():
            continue
        safe_paragraph = html.escape(paragraph.strip(), quote=True).replace(
            "\n", "<br>"
        )
        paragraphs.append(
            f'<p style="margin:0 0 18px;line-height:1.65;">{safe_paragraph}</p>'
        )
    content = "".join(paragraphs)
    return (
        '<!doctype html><html><body style="margin:0;padding:0;'
        'background:#f4f6f9;color:#1a1a2e;">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        'style="background:#f4f6f9;padding:24px 12px;"><tr><td>'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        'style="max-width:640px;margin:0 auto;background:#ffffff;border:1px solid '
        '#e4ebf4;border-radius:12px;"><tr><td style="padding:32px;'
        'font-family:Segoe UI,Helvetica,Arial,sans-serif;">'
        f'<p style="margin:0 0 8px;color:#4f8ef7;font-size:13px;'
        f'font-weight:700;">{safe_address}</p>'
        f'<h1 style="margin:0 0 22px;font-size:24px;line-height:1.3;">'
        f"{safe_subject}</h1>{content}"
        "</td></tr></table></td></tr></table></body></html>"
    )
