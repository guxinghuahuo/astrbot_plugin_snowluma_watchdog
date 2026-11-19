import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from email.errors import MessageError
from email.headerregistry import Address

from yarl import URL


def _string(config, key, default="", *, required=False):
    value = config.get(key, default)
    if not isinstance(value, str) or any(char in value for char in "\r\n"):
        raise ValueError(f"{key} 必须是单行文本")
    if required and not value.strip():
        raise ValueError(f"请填写 {key}")
    return value


def _integer(config, key, default, minimum=1, maximum=86400):
    value = config.get(key, default)
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{key} 必须是 {minimum}～{maximum} 之间的整数")
    return value


def _address(config, key):
    value = _string(config, key, required=True).strip()
    try:
        parsed = Address(addr_spec=value)
    except (MessageError, ValueError):
        raise ValueError(f"{key} 请填写一个邮箱地址") from None
    if not parsed.username or not parsed.domain:
        raise ValueError(f"{key} 请填写一个邮箱地址")
    return parsed.addr_spec


@dataclass(frozen=True)
class MailSettings:
    host: str
    port: int
    security: str
    sender: str
    recipient: str
    password: str = field(repr=False)

    @classmethod
    def from_config(cls, config: Mapping) -> "MailSettings":
        security = _string(config, "smtp_security", "ssl").strip().lower()
        if security not in ("ssl", "starttls"):
            raise ValueError("smtp_security 请选择 ssl 或 starttls")
        return cls(
            host=_string(config, "smtp_host", required=True).strip(),
            port=_integer(config, "smtp_port", 465, maximum=65535),
            security=security,
            sender=_address(config, "smtp_sender"),
            recipient=_address(config, "smtp_recipient"),
            password=_string(config, "smtp_password", required=True),
        )


@dataclass(frozen=True)
class Settings:
    base_url: URL
    access_token: str = field(repr=False)
    expected_qq: str
    mail: MailSettings
    interval: int
    request_timeout: int
    startup_grace: int
    failure_threshold: int
    recovery_threshold: int
    notify_recovery: bool

    @classmethod
    def from_config(cls, config: Mapping) -> "Settings":
        try:
            url = URL(_string(config, "onebot_base_url", "http://127.0.0.1:3000").strip())
            if (
                url.scheme not in ("http", "https") or not url.host
                or url.user is not None or url.query_string or url.fragment
                or not 1 <= url.port <= 65535
            ):
                raise ValueError
        except ValueError:
            raise ValueError("onebot_base_url 请填写 HTTP(S) 基础地址，可包含路径") from None

        token = _string(config, "onebot_access_token", required=True).strip()
        if not re.fullmatch(r"[!-~]+", token):
            raise ValueError("onebot_access_token 必须使用无空格的 ASCII 字符")
        expected = _string(config, "expected_qq").strip()
        if expected and not re.fullmatch(r"[1-9][0-9]*", expected):
            raise ValueError("expected_qq 请填写 QQ 号或留空")
        notify_recovery = config.get("notify_recovery", True)
        if not isinstance(notify_recovery, bool):
            raise ValueError("notify_recovery 请使用开关配置")
        return cls(
            base_url=url.with_path(url.raw_path.rstrip("/") or "/", encoded=True),
            access_token=token,
            expected_qq=expected,
            mail=MailSettings.from_config(config),
            interval=_integer(config, "interval_seconds", 30),
            request_timeout=_integer(config, "request_timeout_seconds", 5, maximum=300),
            startup_grace=_integer(config, "startup_grace_seconds", 60, minimum=0),
            failure_threshold=_integer(config, "failure_threshold", 2),
            recovery_threshold=_integer(config, "recovery_threshold", 2),
            notify_recovery=notify_recovery,
        )
