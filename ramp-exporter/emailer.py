"""Send the Sage CSV as an email attachment via Gmail SMTP."""

import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders


def send_csv(
    gmail_user: str,
    gmail_app_password: str,
    to_address: str | list[str],
    subject: str,
    body_plain: str,
    csv_data: str | None = None,
    filename: str | None = None,
    body_html: str | None = None,
    extra_attachments: list[tuple[str, str]] | None = None,
) -> None:
    """Send an email, optionally with CSV attachments.

    to_address may be a single address string or a list of addresses.
    csv_data/filename are optional — omit both to send a notification-only email.
    extra_attachments is an optional list of (csv_data, filename) tuples.
    """
    recipients = [to_address] if isinstance(to_address, str) else to_address
    outer = MIMEMultipart("mixed")
    outer["From"] = gmail_user
    outer["To"] = ", ".join(recipients)
    outer["Subject"] = subject

    if body_html:
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body_plain, "plain", "utf-8"))
        alt.attach(MIMEText(body_html, "html", "utf-8"))
        outer.attach(alt)
    else:
        outer.attach(MIMEText(body_plain, "plain", "utf-8"))

    attachments = []
    if csv_data is not None and filename is not None:
        attachments.append((csv_data, filename))
    attachments.extend(extra_attachments or [])

    for data, name in attachments:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(data.encode("utf-8"))
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{name}"')
        outer.attach(part)

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(gmail_user, gmail_app_password)
        server.sendmail(gmail_user, recipients, outer.as_string())
