import asyncio
import hashlib
import logging
from datetime import timedelta

import httpx
from sqlalchemy import select, text, update

from ossguard.ai import analyze
from ossguard.config import get_settings
from ossguard.db import EmailNotification, TestResult, Validation, database, now
from ossguard.evidence import Redactor

logger = logging.getLogger(__name__)
TERMINAL = ("COMPLETED", "FAILED", "INTERRUPTED")


def email_payload(settings, job, recipient):
    # Only a factual summary, never raw source, test payloads, secret refs or full logs.
    link = settings.result_base_url.rstrip("/") + "/" + job.id
    return {
        "from": settings.email_from,
        "to": [recipient],
        "subject": f"[OSSGuard] {job.verdict}: {job.package} → {job.target_version}",
        "text": f"검증 ID: {job.id}\n실행: {job.execution_status}\n판정: {job.verdict}\n"
        f"업데이트: {job.package} {job.snapshot['baseline_version']} → {job.target_version}\n"
        f"SHA: {job.snapshot['commit_sha']}\n오류: {job.error_code or '-'}\n"
        f"정리: {job.cleanup_status}\n결과: {link}\n\n결과 조회에는 팀 인증이 필요합니다.",
    }


class ResultWorker:
    def __init__(self, settings, sessions):
        self.settings, self.sessions = settings, sessions

    async def recover(self):
        async with self.sessions.begin() as session:
            await session.execute(
                update(Validation)
                .where(Validation.analysis_status == "RUNNING")
                .values(analysis_status="PENDING")
            )
            inflight = await session.scalars(
                select(EmailNotification)
                .where(EmailNotification.status == "SENDING")
                .with_for_update()
            )
            for notification in inflight:
                # Resend's idempotency window is 24h; leave a safety margin.
                recent = (
                    notification.first_attempt_at
                    and now() - notification.first_attempt_at < timedelta(hours=23)
                )
                notification.status = (
                    "PENDING" if recent and notification.attempts < 3 else "UNKNOWN"
                )
                notification.error = "Result worker restarted during delivery"

    async def process_result(self, client):
        async with self.sessions.begin() as session:
            job = await session.scalar(
                select(Validation)
                .where(
                    Validation.execution_status.in_(TERMINAL),
                    Validation.analysis_status == "PENDING",
                )
                .order_by(Validation.finished_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if not job:
                return False
            job.analysis_status = "RUNNING"
            tests = [
                {"case_id": t.case_id, "environment": t.environment, **t.result}
                for t in await session.scalars(
                    select(TestResult).where(TestResult.validation_id == job.id)
                )
            ]
        analysis_status, explanation = "SKIPPED", {}
        if self.settings.ai_key.get_secret_value() and self.settings.ai_model:
            try:
                explanation = await analyze(self.settings, client, job, tests)
                explanation = Redactor(
                    [
                        self.settings.ai_key.get_secret_value(),
                        self.settings.email_key.get_secret_value(),
                    ]
                ).clean(explanation)
                analysis_status = "SUCCEEDED"
            except Exception:
                analysis_status, explanation = (
                    "FAILED",
                    {"error": "AI analysis unavailable; factual verdict retained"},
                )
        async with self.sessions.begin() as session:
            row = await session.get(Validation, job.id)
            row.analysis_status, row.analysis = analysis_status, explanation
            notification = job.snapshot.get("notification", {})
            if notification.get("enabled"):
                recipient = notification["email"]
                event_key = hashlib.sha256(
                    f"{job.id}:{recipient}:validation.finished".encode()
                ).hexdigest()
                if not await session.scalar(
                    select(EmailNotification.id).where(EmailNotification.event_key == event_key)
                ):
                    session.add(
                        EmailNotification(
                            validation_id=job.id,
                            event_key=event_key,
                            recipient=recipient,
                            payload=email_payload(self.settings, job, recipient),
                        )
                    )
        return True

    async def process_email(self, client):
        async with self.sessions.begin() as session:
            notification = await session.scalar(
                select(EmailNotification)
                .where(
                    EmailNotification.status == "PENDING",
                    EmailNotification.next_attempt_at <= now(),
                )
                .order_by(EmailNotification.next_attempt_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if not notification:
                return False
            if not self.settings.email_key.get_secret_value() or not self.settings.email_from:
                notification.status, notification.error = "FAILED", "EMAIL_NOT_CONFIGURED"
                return True
            if notification.first_attempt_at and now() - notification.first_attempt_at >= timedelta(
                hours=23
            ):
                notification.status, notification.error = (
                    "UNKNOWN",
                    "Idempotency window expired; verify provider before resending",
                )
                return True
            notification.status = "SENDING"
            notification.attempts += 1
            notification.first_attempt_at = notification.first_attempt_at or now()
        status, provider_id, error, retry = "FAILED", None, None, False
        try:
            response = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {self.settings.email_key.get_secret_value()}",
                    "Idempotency-Key": notification.event_key,
                },
                json=notification.payload,
                timeout=self.settings.email_timeout,
            )
            if 200 <= response.status_code < 300:
                provider_id = response.json().get("id")
                status = "ACCEPTED" if provider_id else "UNKNOWN"
            elif response.status_code == 429 or response.status_code >= 500:
                retry, error = True, f"Email provider HTTP {response.status_code}"
            else:
                error = f"Email provider HTTP {response.status_code}"
        except (httpx.HTTPError, ValueError):
            retry, error = (
                True,
                "Email response unavailable; retry only with the same idempotency key",
            )
        if retry:
            status = "PENDING" if notification.attempts < 3 else "UNKNOWN"
        async with self.sessions.begin() as session:
            row = await session.get(EmailNotification, notification.id)
            row.status, row.provider_id, row.error = status, provider_id, error
            row.next_attempt_at = now() + timedelta(seconds=10 * row.attempts)
        return True


async def serve():
    settings = get_settings()
    engine, sessions = database(settings.database_url.get_secret_value())
    worker = ResultWorker(settings, sessions)
    try:
        async with engine.connect() as connection:
            connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
            if not await connection.scalar(text("SELECT pg_try_advisory_lock(73501003)")):
                raise RuntimeError("Another result worker is running")
            await worker.recover()
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                while True:
                    await connection.execute(text("SELECT 1"))
                    processed = await worker.process_result(client)
                    sent = await worker.process_email(client)
                    if not processed and not sent:
                        await asyncio.sleep(settings.poll_seconds)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve())
