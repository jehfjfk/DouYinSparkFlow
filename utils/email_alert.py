import argparse
import mimetypes
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path


SMTP_HOSTS = {
    "qq.com": "smtp.qq.com",
    "163.com": "smtp.163.com",
    "126.com": "smtp.126.com",
    "gmail.com": "smtp.gmail.com",
    "outlook.com": "smtp-mail.outlook.com",
    "hotmail.com": "smtp-mail.outlook.com",
}


def get_mail_config():
    address = os.getenv("MAIL_ADDRESS", "").strip()
    username = (os.getenv("MAIL_USERNAME") or address).strip()
    password = os.getenv("MAIL_PASSWORD", "").strip()
    domain = username.rsplit("@", 1)[-1].lower() if "@" in username else ""
    host = (os.getenv("MAIL_SMTP_HOST") or SMTP_HOSTS.get(domain, "")).strip()
    port = int(os.getenv("MAIL_SMTP_PORT") or "465")
    if not all((address, username, password, host)):
        raise ValueError(
            "邮件提醒未配置完整，需要 MAIL_ADDRESS、MAIL_USERNAME、"
            "MAIL_PASSWORD 和可识别的邮箱域名或 MAIL_SMTP_HOST"
        )
    return address, username, password, host, port


def send_alert(subject, body, attachment_dir=None):
    address, username, password, host, port = get_mail_config()
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = username
    message["To"] = address
    message.set_content(body)

    if attachment_dir:
        for path in sorted(Path(attachment_dir).glob("*.png")):
            mime_type, _ = mimetypes.guess_type(path.name)
            main_type, sub_type = (mime_type or "application/octet-stream").split("/", 1)
            message.add_attachment(
                path.read_bytes(), maintype=main_type, subtype=sub_type, filename=path.name
            )

    with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
        smtp.login(username, password)
        smtp.send_message(message)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--subject", required=True)
    parser.add_argument("--body", required=True)
    parser.add_argument("--attachment-dir")
    args = parser.parse_args()
    send_alert(args.subject, args.body, args.attachment_dir)
    print("邮件提醒已发送")


if __name__ == "__main__":
    main()
