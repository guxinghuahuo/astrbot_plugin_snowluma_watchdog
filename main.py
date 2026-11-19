import asyncio
import hashlib

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .mail import mail_failure_summary, send_mail
from .settings import MailSettings, Settings
from .watchdog import LOG_PREFIX, Watchdog

PLUGIN_NAME = "astrbot_plugin_snowluma_watchdog"
VERSION = "v0.1.4"


class SnowLumaWatchdogPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._task: asyncio.Task | None = None
        self._smtp_test_task: asyncio.Task | None = None
        self._stopping = False

    async def initialize(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        if self.config.get("enabled") is not True:
            logger.info(f"{LOG_PREFIX} {VERSION} 已载入，监控关闭")
            return
        try:
            settings = Settings.from_config(self.config)
        except ValueError as error:
            logger.warning(f"{LOG_PREFIX} {VERSION} 配置未就绪：{error}")
            return

        data_dir = await asyncio.to_thread(StarTools.get_data_dir, PLUGIN_NAME)
        target = f"{settings.base_url}\n{settings.expected_qq}"
        target_id = hashlib.sha256(target.encode("utf-8")).hexdigest()
        # Older state files also recorded OneBot and link-health failures.
        watchdog = Watchdog(settings, data_dir / f"{target_id}.online.state", logger)
        self._task = asyncio.create_task(watchdog.run(), name=PLUGIN_NAME)
        logger.info(f"{LOG_PREFIX} {VERSION} 已启动，启动宽限期 {settings.startup_grace} 秒")

    async def terminate(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
        tasks = {task for task in (self._task, self._smtp_test_task) if task is not None}
        if tasks:
            # Waiting here does not cancel the SMTP thread or interrupt outage state writes.
            await asyncio.wait(tasks)
        self._task = self._smtp_test_task = None

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("snowluma_smtp_test")
    async def smtp_test(self, event: AstrMessageEvent):
        """向已配置的收件邮箱发送一封 SMTP 测试邮件，仅限 AstrBot 管理员。"""
        if not event.is_admin():
            yield event.plain_result("此命令仅限 AstrBot 管理员使用。")
            return
        if self._stopping:
            yield event.plain_result("SnowLuma 监控插件正在停止，请重载完成后再试。")
            return
        if self._smtp_test_task is not None and not self._smtp_test_task.done():
            yield event.plain_result("已有 SMTP 测试正在进行。")
            return
        try:
            settings = MailSettings.from_config(self.config)
        except ValueError as error:
            yield event.plain_result(f"SMTP 配置未就绪：{error}")
            return

        task = asyncio.create_task(self._send_test_email(settings))
        self._smtp_test_task = task
        result = await asyncio.shield(task)
        yield event.plain_result(result)

    async def _send_test_email(self, settings: MailSettings) -> str:
        try:
            await asyncio.to_thread(
                send_mail, settings, f"{LOG_PREFIX} SMTP 测试",
                "这是 SnowLuma 存活监控发出的 SMTP 测试邮件。",
            )
        except (OSError, ValueError) as error:
            failure = f"SMTP 测试失败：{mail_failure_summary(error)}。"
            logger.warning(f"{LOG_PREFIX} {failure}")
            return failure
        logger.info(f"{LOG_PREFIX} SMTP 已接收测试邮件")
        return "SMTP 服务器已接收测试邮件，请到配置的收件邮箱确认。"
