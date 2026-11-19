import asyncio
import hashlib
import importlib
import json
import smtplib
import socket
import ssl
import sys
import types
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock
from zipfile import ZipFile

import aiohttp
import pytest
from aiohttp import web

from astrbot_plugin_snowluma_watchdog import mail, watchdog
from astrbot_plugin_snowluma_watchdog.build_package import ROOT, build
from astrbot_plugin_snowluma_watchdog.probe import ProbeResult, probe
from astrbot_plugin_snowluma_watchdog.settings import Settings
from astrbot_plugin_snowluma_watchdog.watchdog import Phase, Watchdog

CONFIG = {
    "enabled": True,
    "onebot_access_token": "http-secret",
    "smtp_host": "smtp.example.test",
    "smtp_sender": "sender@example.test",
    "smtp_recipient": "alert@example.test",
    "smtp_password": "smtp-secret",
}
ONLINE = ProbeResult(online=True)
OFFLINE = ProbeResult(online=False)
UNKNOWN = ProbeResult(issue="SnowLuma OneBot HTTP 探测超时")


@asynccontextmanager
async def local_onebot(handler):
    app = web.Application()
    app.router.add_post("/onebot/{action}", handler)
    runner = web.AppRunner(app, shutdown_timeout=0.1)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        yield f"http://127.0.0.1:{port}/onebot/"
    finally:
        await runner.cleanup()


def onebot(data, *, status="ok", retcode=0):
    return web.json_response({"status": status, "retcode": retcode, "data": data})


@pytest.mark.parametrize(
    ("http_status", "data", "online", "fragment"),
    [
        (200, {"online": True, "good": True}, True, "QQ 在线（online=true）"),
        (200, {"online": True, "good": False}, True, "QQ 在线（online=true）"),
        (200, {"online": True, "good": "unknown"}, True, "QQ 在线（online=true）"),
        (200, {"online": True}, True, "QQ 在线（online=true）"),
        (200, {"online": False, "good": True}, False, "账号离线"),
        (200, {"online": "true", "good": True}, None, "缺少布尔型"),
        (200, {"good": True}, None, "缺少布尔型"),
        (401, {}, None, "鉴权失败（HTTP 401）"),
        (403, {}, None, "鉴权失败（HTTP 403）"),
        (302, {}, None, "返回 HTTP 302"),
        (503, {}, None, "返回 HTTP 503"),
    ],
)
def test_http_status_and_bearer(http_status, data, online, fragment):
    async def scenario():
        received = []

        async def handler(request):
            received.append((request.match_info["action"], request.headers["Authorization"]))
            assert await request.json() == {}
            if http_status != 200:
                return web.Response(status=http_status, headers={"Location": "get_login_info"})
            return onebot(data)

        async with local_onebot(handler) as url, aiohttp.ClientSession() as session:
            result = await probe(session, Settings.from_config({**CONFIG, "onebot_base_url": url}))
        assert received == [("get_status", "Bearer http-secret")]
        assert fragment in result.description
        assert result.online is online
        assert "http-secret" not in result.description

    asyncio.run(scenario())


@pytest.mark.parametrize(("returned", "online"), [(12345, True), ("12345", True), (54321, None)])
def test_expected_qq(returned, online):
    async def scenario():
        async def handler(request):
            if request.match_info["action"] == "get_status":
                return onebot({"online": True, "good": False})
            return onebot({"user_id": returned})

        async with local_onebot(handler) as url, aiohttp.ClientSession() as session:
            settings = Settings.from_config(
                {**CONFIG, "onebot_base_url": url, "expected_qq": "12345"}
            )
            result = await probe(session, settings)
        assert result.online is online
        if online is None:
            assert "QQ 号与监控配置不符" in result.description
            assert "54321" not in result.description

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ("not JSON http-secret", "有效的 JSON"),
        ("[]", "格式异常"),
        ('{"status":"failed","retcode":100,"data":null}', "OneBot 成功结果"),
        ('{"status":"ok","retcode":false,"data":{}}', "OneBot 成功结果"),
        ("A" * (16 * 1024 + 1), "响应过大"),
    ],
)
def test_invalid_response(body, fragment):
    async def scenario():
        async def handler(_request):
            return web.Response(text=body)

        async with local_onebot(handler) as url, aiohttp.ClientSession() as session:
            result = await probe(session, Settings.from_config({**CONFIG, "onebot_base_url": url}))
        assert result.online is None
        assert fragment in result.description
        assert "http-secret" not in result.description

    asyncio.run(scenario())


def test_timeout():
    async def scenario():
        async def handler(_request):
            await asyncio.Event().wait()

        async with local_onebot(handler) as url, aiohttp.ClientSession() as session:
            settings = Settings.from_config({**CONFIG, "onebot_base_url": url})
            result = await probe(session, replace(settings, request_timeout=0.05))
        assert result.online is None
        assert "超时" in result.description

    asyncio.run(scenario())


def test_debounce_deduplication_and_recovery_during_one_run(tmp_path, monkeypatch):
    async def scenario():
        settings = Settings.from_config({**CONFIG, "expected_qq": "12345"})
        state_path = tmp_path / "watchdog.state"
        send = Mock()
        monkeypatch.setattr(watchdog, "send_mail", send)
        monitor = Watchdog(settings, state_path, Mock())
        for sample in (OFFLINE, ONLINE, OFFLINE):
            await monitor._apply_sample(sample)
        send.assert_not_called()
        for _ in range(3):
            await monitor._apply_sample(OFFLINE)
        assert send.call_count == 1
        assert send.call_args.args[1] == "[SnowLuma Watchdog] QQ 离线"
        assert send.call_args.args[2] == "监控状态：QQ 账号离线（online=false）"
        assert state_path.read_text().strip() == Phase.ALERT_SENT.value

        for sample in (OFFLINE, OFFLINE, ONLINE, OFFLINE, ONLINE):
            await monitor._apply_sample(sample)
        assert send.call_count == 1
        await monitor._apply_sample(ONLINE)
        assert send.call_count == 2
        assert "恢复" in send.call_args.args[1]
        assert send.call_args.args[2] == "监控状态：QQ 在线（online=true）"
        await monitor._apply_sample(ONLINE)
        assert send.call_count == 2
        assert monitor.phase == Phase.HEALTHY

        await monitor._apply_sample(OFFLINE)
        await monitor._apply_sample(OFFLINE)
        assert send.call_count == 3

    asyncio.run(scenario())


def test_previous_alert_does_not_suppress_failure_after_restart(tmp_path, monkeypatch):
    async def scenario():
        settings = Settings.from_config(CONFIG)
        state_path = tmp_path / "watchdog.state"
        send = Mock()
        monkeypatch.setattr(watchdog, "send_mail", send)
        before = Watchdog(settings, state_path, Mock())
        await before._apply_sample(OFFLINE)
        await before._apply_sample(OFFLINE)
        assert send.call_count == 1
        assert state_path.read_text().strip() == Phase.ALERT_SENT.value

        # SnowLuma can recover and fail again while the monitor is stopped.
        # Sending the same failure result ensures the old reason cannot decide continuity.
        log = Mock()
        after = Watchdog(settings, state_path, log)
        await after.load_state()
        await after._apply_sample(OFFLINE)
        assert send.call_count == 1
        await after._apply_sample(OFFLINE)
        assert send.call_count == 2
        assert "QQ 离线" in send.call_args.args[1]
        assert "监控重启后确认离线" in log.warning.call_args.args[0]
        await after._apply_sample(OFFLINE)
        assert send.call_count == 2

    asyncio.run(scenario())


def test_unknown_breaks_consecutive_offline_and_online_samples(tmp_path, monkeypatch):
    async def scenario():
        send = Mock()
        monkeypatch.setattr(watchdog, "send_mail", send)
        monitor = Watchdog(Settings.from_config(CONFIG), tmp_path / "watchdog.state", Mock())
        auth = ProbeResult(issue="get_status 鉴权失败（HTTP 401）")
        for sample in (OFFLINE, UNKNOWN, auth, UNKNOWN, OFFLINE):
            await monitor._apply_sample(sample)
        send.assert_not_called()
        await monitor._apply_sample(OFFLINE)
        assert send.call_count == 1
        for sample in (ONLINE, UNKNOWN, ONLINE):
            await monitor._apply_sample(sample)
        assert send.call_count == 1
        assert monitor.phase == Phase.ALERT_SENT
        await monitor._apply_sample(ONLINE)
        assert send.call_count == 2
        assert "恢复" in send.call_args.args[1]

    asyncio.run(scenario())


def test_mail_retry_and_recovery_toggle(tmp_path, monkeypatch):
    async def scenario():
        now = [100]
        monkeypatch.setattr(watchdog, "time", types.SimpleNamespace(monotonic=lambda: now[0]))
        send = Mock(side_effect=[smtplib.SMTPAuthenticationError(535, b"smtp-secret"), None])
        monkeypatch.setattr(watchdog, "send_mail", send)
        settings = Settings.from_config({**CONFIG, "notify_recovery": False})
        log = Mock()
        monitor = Watchdog(settings, tmp_path / "watchdog.state", log)
        for sample in (OFFLINE, OFFLINE, OFFLINE):
            await monitor._apply_sample(sample)
        assert send.call_count == 1
        assert monitor.phase == Phase.OUTAGE
        assert "SMTP 鉴权失败" in log.warning.call_args.args[0]
        assert "SMTP 535" in log.warning.call_args.args[0]
        assert "smtp-secret" not in str(log.mock_calls)
        now[0] += watchdog.SMTP_RETRY_SECONDS
        await monitor._apply_sample(OFFLINE)
        assert send.call_count == 2
        for _ in range(2):
            await monitor._apply_sample(ONLINE)
        assert send.call_count == 2
        assert monitor.phase == Phase.HEALTHY

    asyncio.run(scenario())


def test_cancel_waits_for_delivery_and_saves_state(tmp_path, monkeypatch):
    async def scenario():
        settings = Settings.from_config({**CONFIG, "failure_threshold": 1})
        state_path = tmp_path / "watchdog.state"
        monitor = Watchdog(settings, state_path, Mock())
        delivering = asyncio.Event()
        complete = asyncio.Event()
        to_thread = asyncio.to_thread

        async def thread(function, *args, **kwargs):
            if function is watchdog.send_mail:
                delivering.set()
                await complete.wait()
                return None
            return await to_thread(function, *args, **kwargs)

        monkeypatch.setattr(watchdog.asyncio, "to_thread", thread)
        task = asyncio.create_task(monitor._finish_sample(OFFLINE))
        await delivering.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        complete.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert state_path.read_text().strip() == Phase.ALERT_SENT.value

    asyncio.run(scenario())


@pytest.mark.parametrize(("mode", "port"), [("ssl", 465), ("starttls", 587)])
def test_smtp_uses_tls_before_auth(monkeypatch, mode, port):
    client = Mock()
    ssl_constructor, plain_constructor = Mock(return_value=client), Mock(return_value=client)
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", ssl_constructor)
    monkeypatch.setattr(mail.smtplib, "SMTP", plain_constructor)
    settings = Settings.from_config({**CONFIG, "smtp_security": mode, "smtp_port": port}).mail
    mail.send_mail(settings, "[SnowLuma Watchdog] QQ 离线", OFFLINE.description)
    assert [call[0] for call in client.mock_calls] == (
        ["login", "send_message", "close"]
        if mode == "ssl"
        else ["starttls", "login", "send_message", "close"]
    )
    if mode == "ssl":
        assert ssl_constructor.call_args.args == (settings.host, 465)
        assert ssl_constructor.call_args.kwargs["context"].check_hostname
        plain_constructor.assert_not_called()
    else:
        assert plain_constructor.call_args.args == (settings.host, 587)
        assert client.starttls.call_args.kwargs["context"].check_hostname
        ssl_constructor.assert_not_called()
    client.login.assert_called_once_with(settings.sender, "smtp-secret")
    message = client.send_message.call_args.args[0]
    assert message["To"] == settings.recipient
    assert "QQ 账号离线" in message.get_content()
    assert "smtp-secret" not in message.get_content()


def test_config_defaults_and_secret_validation():
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    assert not schema["enabled"]["default"]
    assert schema["onebot_access_token"]["secret"]
    assert schema["smtp_password"]["secret"]
    assert schema["onebot_access_token"]["default"] == schema["smtp_password"]["default"] == ""
    defaults = {name: field["default"] for name, field in schema.items()}
    assert Settings.from_config({**defaults, **CONFIG}) == Settings.from_config(CONFIG)
    assert str(Settings.from_config(CONFIG).base_url) == "http://127.0.0.1:3000/"
    assert "http-secret" not in repr(Settings.from_config(CONFIG))
    assert "smtp-secret" not in repr(Settings.from_config(CONFIG))
    for key, value in (
        ("onebot_base_url", "http://user:private@example.test"),
        ("onebot_base_url", "http://example.test:0"),
        ("smtp_recipient", "secret@"),
        ("onebot_access_token", "secret\nheader"),
        ("failure_threshold", 0),
    ):
        with pytest.raises(ValueError, match=key) as error:
            Settings.from_config({**CONFIG, key: value})
        assert "secret" not in str(error.value)
        assert "private" not in str(error.value)


@pytest.fixture
def plugin_entry(tmp_path, monkeypatch):
    class Star:
        def __init__(self, context):
            self.context = context

    def get_data_dir(name):
        path = tmp_path / "plugin_data" / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def decorator(attribute):
        def configure(value):
            def register(handler):
                setattr(handler, attribute, value)
                return handler

            return register

        return configure

    package = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig, api.logger = dict, Mock()
    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = object
    event.filter = types.SimpleNamespace(
        command=decorator("command"),
        permission_type=decorator("permission"),
        PermissionType=types.SimpleNamespace(ADMIN=object()),
    )
    star = types.ModuleType("astrbot.api.star")
    star.Context = object
    star.Star, star.StarTools = Star, types.SimpleNamespace(get_data_dir=get_data_dir)
    for module in (package, api, event, star):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    module_name = "astrbot_plugin_snowluma_watchdog.main"
    try:
        yield importlib.import_module(module_name)
    finally:
        sys.modules.pop(module_name, None)


def test_plugin_upgrade_ignores_legacy_alert_and_reports_actual_offline(
    tmp_path, monkeypatch, plugin_entry,
):
    entry = plugin_entry
    send = Mock()
    monkeypatch.setattr(watchdog, "send_mail", send)

    async def scenario():
        online = True

        async def handler(request):
            if request.headers["Authorization"] != "Bearer http-secret":
                return web.Response(status=401)
            return onebot({"online": online, "good": False})

        async def wait_until(predicate):
            while not predicate():
                await asyncio.sleep(0.01)

        def initial_probe_contains(fragment):
            return any(
                "首次探测" in call.args[0] and fragment in call.args[0]
                for call in entry.logger.info.call_args_list
            )

        context = object()
        async with local_onebot(handler) as url:
            config = {
                **CONFIG,
                "onebot_base_url": url,
                "startup_grace_seconds": 0,
                "interval_seconds": 1,
                "failure_threshold": 1,
                "recovery_threshold": 1,
            }
            state_dir = tmp_path / "plugin_data" / entry.PLUGIN_NAME
            state_dir.mkdir(parents=True)
            target = f"{Settings.from_config(config).base_url}\n"
            legacy_state = state_dir / f"{hashlib.sha256(target.encode()).hexdigest()}.state"
            legacy_state.write_text("alert_sent\n", encoding="ascii")
            bad_token = {**config, "onebot_access_token": "invalid"}
            plugin = entry.SnowLumaWatchdogPlugin(context, bad_token)
            await plugin.initialize()
            task = plugin._task
            try:
                assert task is not None and not task.done()
                await plugin.initialize()
                assert task is plugin._task
                await asyncio.wait_for(
                    wait_until(lambda: initial_probe_contains("鉴权失败（HTTP 401）")), 5
                )
                send.assert_not_called()
            finally:
                await plugin.terminate()
            assert task.cancelled() and plugin._task is None

            monitor = entry.SnowLumaWatchdogPlugin(context, config)
            await monitor.initialize()
            try:
                await asyncio.wait_for(wait_until(lambda: initial_probe_contains("online=true")), 5)
                send.assert_not_called()
                online = False
                await asyncio.wait_for(wait_until(lambda: send.call_count == 1), 5)
                assert "online=false" in send.call_args.args[2]
            finally:
                await monitor.terminate()
            assert next(state_dir.glob("*.online.state")).read_text().strip() == "alert_sent"
            assert legacy_state.read_text().strip() == "alert_sent"

            online = True
            restored = entry.SnowLumaWatchdogPlugin(context, config)
            await restored.initialize()
            try:
                await asyncio.wait_for(wait_until(lambda: send.call_count == 2), 5)
                assert "恢复" in send.call_args.args[1]
            finally:
                await restored.terminate()

        disabled = entry.SnowLumaWatchdogPlugin(context, {})
        await disabled.initialize()
        assert disabled._task is None
        await disabled.terminate()

    asyncio.run(scenario())


def test_admin_can_test_smtp_with_monitor_disabled(plugin_entry, monkeypatch, tmp_path):
    send = Mock(side_effect=[smtplib.SMTPAuthenticationError(535, b"smtp-secret"), None])
    monkeypatch.setattr(plugin_entry, "send_mail", send)
    plugin_type = plugin_entry.SnowLumaWatchdogPlugin
    assert plugin_type.smtp_test.command == "snowluma_smtp_test"
    assert plugin_type.smtp_test.permission == plugin_entry.filter.PermissionType.ADMIN
    config = {key: value for key, value in CONFIG.items() if key.startswith("smtp_")}
    plugin = plugin_type(object(), config)

    async def scenario():
        event = Mock()
        event.is_admin.return_value = False
        event.plain_result.side_effect = lambda text: text

        await plugin.initialize()
        assert [reply async for reply in plugin.smtp_test(event)] == [
            "此命令仅限 AstrBot 管理员使用。"
        ]
        send.assert_not_called()

        event.is_admin.return_value = True
        failure, = [reply async for reply in plugin.smtp_test(event)]
        assert "SMTP 鉴权失败" in failure and "SMTP 535" in failure
        assert "smtp-secret" not in failure
        log = plugin_entry.logger.warning.call_args.args[0]
        assert "SMTP 535" in log and "smtp-secret" not in log

        success, = [reply async for reply in plugin.smtp_test(event)]
        assert "SMTP 服务器已接收测试邮件" in success
        settings, subject, _ = send.call_args.args
        assert settings.recipient == config["smtp_recipient"]
        assert subject == "[SnowLuma Watchdog] SMTP 测试"
        assert not (tmp_path / "plugin_data").exists()
        await plugin.terminate()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("error", "summary"),
    [
        (socket.gaierror("host with private details"), "无法解析 SMTP 服务器地址"),
        (TimeoutError("private details"), "SMTP 连接或请求超时"),
        (ssl.SSLError("private details"), "SMTP TLS 握手失败"),
        (smtplib.SMTPRecipientsRefused({"private@example.test": (550, b"private")}), "收件邮箱"),
    ],
)
def test_smtp_errors_explain_failure_without_leaking_provider_response(error, summary):
    message = mail.mail_failure_summary(error)
    assert summary in message
    assert "private" not in message


def test_archive_matches_astrbot_upload_layout(tmp_path):
    output = build(tmp_path)
    assert output.parent.name == "v0.1.4"
    assert output.name == "astrbot_plugin_snowluma_watchdog.zip"
    with ZipFile(output) as archive:
        root = archive.infolist()[0]
        assert root.is_dir() and root.filename == f"{output.stem}/"
        assert archive.testzip() is None
        archive.extractall(tmp_path / "extracted")
        assert all(Path(name).parts[0] == output.stem for name in archive.namelist())
    extracted = tmp_path / "extracted" / root.filename
    assert "version: v0.1.4" in (extracted / "metadata.yaml").read_text(encoding="utf-8")
    assert (extracted / "main.py").is_file()
    assert not (extracted / "tests").exists()
    assert hashlib.sha256(output.read_bytes()).hexdigest() in output.with_suffix(
        ".zip.sha256"
    ).read_text()
