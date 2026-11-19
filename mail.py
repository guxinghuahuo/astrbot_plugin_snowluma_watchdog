import smtplib
import socket
import ssl
from contextlib import suppress
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime

from .settings import MailSettings


def mail_failure_summary(error: Exception) -> str:
    reasons = (
        (smtplib.SMTPAuthenticationError, "SMTP 鉴权失败，请检查发件账号和授权码"),
        (ssl.SSLCertVerificationError, "SMTP 证书校验失败，请检查服务器地址和证书"),
        (ssl.SSLError, "SMTP TLS 握手失败，请检查端口和加密方式"),
        (smtplib.SMTPNotSupportedError, "SMTP 服务器不支持所选加密或登录方式"),
        (smtplib.SMTPRecipientsRefused, "SMTP 服务器拒绝了收件邮箱"),
        (smtplib.SMTPSenderRefused, "SMTP 服务器拒绝了发件邮箱"),
        (smtplib.SMTPDataError, "SMTP 服务器拒绝接收邮件内容"),
        (socket.gaierror, "无法解析 SMTP 服务器地址"),
        (TimeoutError, "SMTP 连接或请求超时"),
        (smtplib.SMTPException, "SMTP 会话失败，请检查服务器地址、端口和加密方式"),
        (OSError, "SMTP 网络连接失败，请检查服务器地址、端口和网络"),
    )
    summary = next(
        (text for error_type, text in reasons if isinstance(error, error_type)),
        "SMTP 发信参数无效，请检查邮箱配置",
    )
    if isinstance(error, smtplib.SMTPResponseException):
        code = error.smtp_code
        if type(code) is int and 100 <= code <= 599:
            summary += f"（SMTP {code}）"
    return summary


def send_mail(settings: MailSettings, subject: str, body: str) -> None:
    message = EmailMessage()
    now = datetime.now().astimezone()
    message["From"] = settings.sender
    message["To"] = settings.recipient
    message["Subject"] = subject
    message["Date"] = format_datetime(now)
    message.set_content(f"时间：{now.isoformat(timespec='seconds')}\n{body}\n")

    tls_context = ssl.create_default_context()
    if settings.security == "ssl":
        client = smtplib.SMTP_SSL(settings.host, settings.port, timeout=10, context=tls_context)
    else:
        client = smtplib.SMTP(settings.host, settings.port, timeout=10)
    try:
        if settings.security == "starttls":
            client.starttls(context=tls_context)
        client.login(settings.sender, settings.password)
        client.send_message(message)
    finally:
        # A failed QUIT after an acknowledged message must not trigger a retry.
        with suppress(OSError):
            client.close()
