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
    to_address: str,
    subject: str,
    body: str,
    csv_data: str,
    filename: str,
) -> None:
    msg = MIMEMultipart()
    msg["From"] = gmail_user
    msg["To"] = to_address
    msg["Subject"] = subject

    msg.attach(MIMEText(body, "plain"))

    part = MIMEBase("application", "octet-stream")
    part.set_payload(csv_data.encode("utf-8"))
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
    msg.attach(part)

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(gmail_user, gmail_app_password)
        server.sendmail(gmail_user, to_address, msg.as_string())
