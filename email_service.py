"""Transactional email delivery (SendGrid API preferred, SMTP fallback)."""

from __future__ import annotations

import json
import logging
import smtplib
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage

import config

logger = logging.getLogger(__name__)


def email_configured() -> bool:
    if (config.SENDGRID_API_KEY or "").strip():
        return True
    return bool((config.SMTP_HOST or "").strip() and (config.SMTP_FROM_EMAIL or "").strip())


def send_email(*, to_email: str, subject: str, text_body: str, html_body: str) -> bool:
    """
    Send a transactional email. Returns True on accepted handoff.
    Never logs recipients' message bodies containing tokens.
    """
    to_email = (to_email or "").strip().lower()
    if not to_email or "@" not in to_email:
        return False
    from_email = (config.SMTP_FROM_EMAIL or config.CONTACT_EMAIL or "").strip()
    from_name = config.PRODUCT_NAME
    if config.SENDGRID_API_KEY:
        return _send_sendgrid(to_email, from_email, from_name, subject, text_body, html_body)
    if config.SMTP_HOST:
        return _send_smtp(to_email, from_email, from_name, subject, text_body, html_body)
    logger.error("Email not configured: set SENDGRID_API_KEY or SMTP_HOST")
    return False


def _send_sendgrid(to_email, from_email, from_name, subject, text_body, html_body) -> bool:
    payload = {
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": {"email": from_email, "name": from_name},
        "subject": subject,
        "content": [
            {"type": "text/plain", "value": text_body},
            {"type": "text/html", "value": html_body},
        ],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {config.SENDGRID_API_KEY.strip()}",
            "Content-Type": "application/json",
            "User-Agent": "TopAI-Real-Estate-Tools/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            ok = 200 <= resp.status < 300 or resp.status == 202
            if ok:
                logger.info("SendGrid accepted password-reset email handoff")
            return ok
    except urllib.error.HTTPError as exc:
        logger.error("SendGrid email failed http=%s", exc.code)
        return False
    except Exception:
        logger.exception("SendGrid email failed")
        return False


def _send_smtp(to_email, from_email, from_name, subject, text_body, html_body) -> bool:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{from_email}>"
    msg["To"] = to_email
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    try:
        context = ssl.create_default_context()
        port = int(config.SMTP_PORT or 587)
        with smtplib.SMTP(config.SMTP_HOST, port, timeout=20) as smtp:
            smtp.ehlo()
            if config.SMTP_USE_TLS:
                smtp.starttls(context=context)
                smtp.ehlo()
            if config.SMTP_USERNAME:
                smtp.login(config.SMTP_USERNAME, config.SMTP_PASSWORD or "")
            smtp.send_message(msg)
        logger.info("SMTP accepted password-reset email handoff")
        return True
    except Exception:
        logger.exception("SMTP email failed")
        return False


def send_password_reset_email(*, to_email: str, reset_url: str, expires_minutes: int) -> bool:
    product = config.PRODUCT_NAME
    support = config.CONTACT_EMAIL
    subject = f"Create or reset your {product} password"
    text_body = (
        f"{product}\n\n"
        "Use the secure link below to create or reset your password:\n\n"
        f"{reset_url}\n\n"
        f"This link expires in {expires_minutes} minutes and can be used only once.\n"
        "If you did not request this, you can ignore this email.\n\n"
        f"Support: {support}\n"
    )
    html_body = f"""<!DOCTYPE html>
<html><body style="font-family:Segoe UI,Helvetica,Arial,sans-serif;background:#f4f6f9;padding:24px;color:#1a1a2e;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:560px;margin:0 auto;background:#ffffff;border-radius:12px;padding:28px;border:1px solid #e4ebf4;">
    <tr><td>
      <h1 style="margin:0 0 8px;font-size:22px;color:#10243f;">{product}</h1>
      <p style="margin:0 0 18px;color:#556;">Operated by {config.LEGAL_ENTITY_NAME}</p>
      <p style="margin:0 0 18px;line-height:1.55;">
        Use the secure link below to create or reset your {product} password.
      </p>
      <p style="margin:0 0 22px;">
        <a href="{reset_url}" style="display:inline-block;background:#2f6fed;color:#fff;text-decoration:none;font-weight:700;padding:12px 18px;border-radius:8px;">
          Create or reset password
        </a>
      </p>
      <p style="margin:0 0 10px;font-size:13px;color:#667;line-height:1.5;">
        This link expires in {expires_minutes} minutes and can be used only once.
        If the button does not work, copy and paste this URL into your browser:
      </p>
      <p style="margin:0 0 18px;font-size:12px;word-break:break-all;color:#2f6fed;">{reset_url}</p>
      <p style="margin:0 0 8px;font-size:13px;color:#667;">
        If you did not request this, you can ignore this email. Your password will not change.
      </p>
      <p style="margin:16px 0 0;font-size:13px;color:#667;">
        Support: <a href="mailto:{support}" style="color:#2f6fed;">{support}</a>
      </p>
    </td></tr>
  </table>
</body></html>"""
    return send_email(
        to_email=to_email,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
    )
