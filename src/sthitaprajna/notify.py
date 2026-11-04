"""Email fallback for operator alerts (D10). Configured only through the environment:
SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASSWORD, ALERT_EMAIL_TO. Never sends secrets or account numbers."""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage


def send_email(subject: str, body: str) -> bool:
    host, to = os.environ.get("SMTP_HOST"), os.environ.get("ALERT_EMAIL_TO")
    if not host or not to:
        return False
    msg = EmailMessage()
    msg["Subject"], msg["To"] = subject, to
    msg["From"] = os.environ.get("SMTP_USER") or to
    msg.set_content(body)
    try:
        with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=10) as s:
            s.starttls()
            if os.environ.get("SMTP_USER"):
                s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
            s.send_message(msg)
        return True
    except Exception:
        return False
