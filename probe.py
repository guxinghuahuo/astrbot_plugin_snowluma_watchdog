import asyncio
import json
from dataclasses import dataclass

import aiohttp

from .settings import Settings

MAX_RESPONSE_BYTES = 16 * 1024


class ProbeError(Exception):
    """A OneBot failure with a message safe to log."""


@dataclass(frozen=True)
class ProbeResult:
    online: bool | None = None
    issue: str | None = None

    @property
    def description(self) -> str:
        if self.online is None:
            return f"QQ 在线状态未知：{self.issue}" if self.issue else "QQ 在线状态未知"
        return "QQ 在线（online=true）" if self.online else "QQ 账号离线（online=false）"


async def probe(session: aiohttp.ClientSession, settings: Settings) -> ProbeResult:
    try:
        return await asyncio.wait_for(_check(session, settings), settings.request_timeout)
    except asyncio.TimeoutError:
        return ProbeResult(issue="SnowLuma OneBot HTTP 探测超时")
    except ProbeError as error:
        return ProbeResult(issue=str(error))
    except aiohttp.ClientError:
        return ProbeResult(issue="SnowLuma OneBot HTTP 连接或响应传输失败")


async def _check(session: aiohttp.ClientSession, settings: Settings) -> ProbeResult:
    status = await _call(session, settings, "get_status")
    online = status.get("online")
    if type(online) is not bool:
        raise ProbeError("get_status 响应缺少布尔型 online")
    if online and settings.expected_qq:
        login = await _call(session, settings, "get_login_info")
        user_id = login.get("user_id")
        if type(user_id) not in (int, str) or str(user_id) != settings.expected_qq:
            raise ProbeError("SnowLuma 返回的 QQ 号与监控配置不符")
    return ProbeResult(online=online)


async def _call(session: aiohttp.ClientSession, settings: Settings, action: str) -> dict:
    path = f"{settings.base_url.raw_path.rstrip('/')}/{action}"
    url = settings.base_url.with_path(path, encoded=True)
    async with session.post(
        url, json={}, headers={"Authorization": f"Bearer {settings.access_token}"},
        allow_redirects=False,
    ) as response:
        if response.status in (401, 403):
            raise ProbeError(
                f"{action} 鉴权失败（HTTP {response.status}），请检查 HTTP Access Token"
            )
        if response.status != 200:
            raise ProbeError(f"{action} 返回 HTTP {response.status}，请检查 OneBot 地址和服务")
        try:
            await response.content.readexactly(MAX_RESPONSE_BYTES + 1)
        except asyncio.IncompleteReadError as error:
            body = error.partial
        else:
            raise ProbeError(f"{action} 响应过大")
    try:
        result = json.loads(body)
    except (ValueError, RecursionError):
        raise ProbeError(f"{action} 响应不是有效的 JSON") from None
    if not isinstance(result, dict):
        raise ProbeError(f"{action} 响应格式异常")
    retcode = result.get("retcode")
    if (
        result.get("status") != "ok" or type(retcode) is not int or retcode != 0
        or not isinstance(result.get("data"), dict)
    ):
        raise ProbeError(f"{action} 未返回有效的 OneBot 成功结果")
    return result["data"]
