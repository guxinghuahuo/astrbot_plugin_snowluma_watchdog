import asyncio
import time
from enum import Enum
from pathlib import Path

import aiohttp

from .mail import mail_failure_summary, send_mail
from .probe import ProbeResult, probe
from .settings import Settings

SMTP_RETRY_SECONDS = 300
LOG_PREFIX = "[SnowLuma Watchdog]"


class Phase(str, Enum):
    HEALTHY = "healthy"
    OUTAGE = "outage"
    ALERT_SENT = "alert_sent"
    RECOVERY_PENDING = "recovery_pending"


class Watchdog:
    def __init__(self, settings: Settings, state_path: Path, logger):
        self.settings = settings
        self.state_path = state_path
        self.logger = logger
        self.phase = Phase.HEALTHY
        self.saved_phase = Phase.HEALTHY
        self.restored_outage = False
        self.last_online: bool | None = None
        self.streak = 0
        self.next_mail_attempt = 0.0

    async def run(self) -> None:
        try:
            await self.load_state()
            await asyncio.sleep(self.settings.startup_grace)
            async with aiohttp.ClientSession(trust_env=False) as session:
                while True:
                    started = time.monotonic()
                    result = await probe(session, self.settings)
                    await self._finish_sample(result)
                    elapsed = time.monotonic() - started
                    await asyncio.sleep(max(0, self.settings.interval - elapsed))
        except Exception as error:
            self.logger.error(f"{LOG_PREFIX} 监控任务已停止（{type(error).__name__}）")

    async def load_state(self) -> None:
        try:
            raw = await asyncio.to_thread(self.state_path.read_text, encoding="ascii")
            self.phase = self.saved_phase = Phase(raw.strip())
            # The saved outage can inform a recovery mail. It cannot establish
            # that failures after an unobserved interval belong to that outage.
            self.restored_outage = self.phase in (Phase.OUTAGE, Phase.ALERT_SENT)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as error:
            self.logger.warning(f"{LOG_PREFIX} 无法读取告警状态（{type(error).__name__}）")

    async def _finish_sample(self, result: ProbeResult) -> None:
        # SMTP runs in a thread. Finish delivery and the state write before plugin reload.
        sample = asyncio.create_task(self._apply_sample(result))
        try:
            await asyncio.shield(sample)
        except asyncio.CancelledError:
            await sample
            raise

    async def _apply_sample(self, result: ProbeResult) -> None:
        if self.streak == 0:
            self.logger.info(f"{LOG_PREFIX} 首次探测：{result.description}")
        elif result.online is None and self.last_online is not None:
            self.logger.warning(f"{LOG_PREFIX} {result.description}")
        else:
            self.logger.debug(f"{LOG_PREFIX} {result.description}")
        self.streak = self.streak + 1 if self.last_online is result.online else 1
        self.last_online = result.online

        if result.online is True and self.streak >= self.settings.recovery_threshold:
            if self.phase in (Phase.OUTAGE, Phase.ALERT_SENT):
                self.logger.info(f"{LOG_PREFIX} SnowLuma 已恢复")
                send_recovery = self.phase == Phase.ALERT_SENT and self.settings.notify_recovery
                self._transition(Phase.RECOVERY_PENDING if send_recovery else Phase.HEALTHY)
            if self.phase == Phase.RECOVERY_PENDING:
                if self.settings.notify_recovery:
                    await self._notify(result, Phase.HEALTHY)
                else:
                    self._transition(Phase.HEALTHY)
        if result.online is False and self.streak >= self.settings.failure_threshold:
            if self.restored_outage or self.phase in (Phase.HEALTHY, Phase.RECOVERY_PENDING):
                confirmation = "监控重启后确认离线" if self.restored_outage else "确认离线"
                self.logger.warning(f"{LOG_PREFIX} {confirmation}：{result.description}")
                self._transition(Phase.OUTAGE)
            if self.phase == Phase.OUTAGE:
                await self._notify(result, Phase.ALERT_SENT)

        if self.phase != self.saved_phase:
            try:
                await asyncio.to_thread(self._write_state, self.phase)
            except OSError as error:
                self.logger.warning(f"{LOG_PREFIX} 无法保存告警状态（{type(error).__name__}）")
            else:
                self.saved_phase = self.phase

    def _transition(self, phase: Phase) -> None:
        self.phase = phase
        self.restored_outage = False
        self.next_mail_attempt = 0

    async def _notify(self, result: ProbeResult, delivered_phase: Phase) -> None:
        if time.monotonic() < self.next_mail_attempt:
            return
        kind = "恢复" if result.online is True else "离线"
        subject = f"{LOG_PREFIX} QQ {kind}"
        body = f"监控状态：{result.description}"
        try:
            await asyncio.to_thread(send_mail, self.settings.mail, subject, body)
        except (OSError, ValueError) as error:
            self.next_mail_attempt = time.monotonic() + SMTP_RETRY_SECONDS
            self.logger.warning(
                f"{LOG_PREFIX} {kind}邮件发送失败：{mail_failure_summary(error)}。"
                f"{SMTP_RETRY_SECONDS} 秒后可重试"
            )
        else:
            self._transition(delivered_phase)
            self.logger.info(f"{LOG_PREFIX} SMTP 已接收{kind}邮件")

    def _write_state(self, phase: Phase) -> None:
        temp_path = self.state_path.with_suffix(".tmp")
        temp_path.write_text(f"{phase.value}\n", encoding="ascii")
        temp_path.replace(self.state_path)
