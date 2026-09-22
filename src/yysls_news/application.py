from __future__ import annotations

from dataclasses import dataclass

from yysls_news.collectors.bilibili.auth import BilibiliQrLoginClient
from yysls_news.collectors.bilibili.lifecycle import BilibiliCredentialLifecycle
from yysls_news.collectors.yysls.client import DEFAULT_NEWS_URL
from yysls_news.config import Settings
from yysls_news.delivery.qqbot.gateway import QQBotGatewayListener
from yysls_news.security.secrets import SecretBox
from yysls_news.services.credentials import CredentialService
from yysls_news.services.ingestion import MonitorService
from yysls_news.services.qq_binding import QQBindingService
from yysls_news.services.runtime_config import RuntimeConfigService
from yysls_news.services.runtime_logs import RuntimeLogBuffer
from yysls_news.storage.database import Database
from yysls_news.storage.repositories import (
    AppSettingsRepository,
    BilibiliSubscriptionRepository,
    ContentRepository,
    CredentialRepository,
    DeliveryTargetRepository,
    DeliveryTaskRepository,
    PushHistoryRepository,
    QQBindingRepository,
    WatchSourceRepository,
)


@dataclass
class ApplicationContext:
    settings: Settings
    database: Database
    app_settings: AppSettingsRepository
    credentials_repository: CredentialRepository
    credentials: CredentialService | None
    subscriptions: BilibiliSubscriptionRepository
    sources: WatchSourceRepository
    contents: ContentRepository
    targets: DeliveryTargetRepository
    tasks: DeliveryTaskRepository
    push_history: PushHistoryRepository
    runtime_config: RuntimeConfigService
    runtime_logs: RuntimeLogBuffer
    qq_bindings: QQBindingService
    qq_listener: QQBotGatewayListener
    qr_login: BilibiliQrLoginClient

    @classmethod
    def create(cls, settings: Settings | None = None) -> ApplicationContext:
        settings = settings or Settings.from_env()
        database = Database(settings.database_path)
        database.initialize()
        app_settings = AppSettingsRepository(database)
        secret_box = SecretBox(settings.encryption_key) if settings.encryption_key else None
        credentials_repository = CredentialRepository(database)
        credentials = CredentialService(credentials_repository, secret_box) if secret_box else None
        sources = WatchSourceRepository(database)
        targets = DeliveryTargetRepository(database)
        runtime_config = RuntimeConfigService(settings, app_settings, secret_box)
        runtime_logs = RuntimeLogBuffer()
        qq_bindings = QQBindingService(
            repository=QQBindingRepository(database),
            targets=targets,
        )
        if not sources.exists("yysls", "official"):
            sources.upsert(
                source_type="yysls",
                source_key="official",
                name="燕云十六声官网新闻",
                url=DEFAULT_NEWS_URL,
                category="最新",
                enabled=True,
                poll_interval_seconds=settings.yysls_poll_interval_seconds,
            )
        return cls(
            settings=settings,
            database=database,
            app_settings=app_settings,
            credentials_repository=credentials_repository,
            credentials=credentials,
            subscriptions=BilibiliSubscriptionRepository(database),
            sources=sources,
            contents=ContentRepository(database),
            targets=targets,
            tasks=DeliveryTaskRepository(database),
            push_history=PushHistoryRepository(database),
            runtime_config=runtime_config,
            runtime_logs=runtime_logs,
            qq_bindings=qq_bindings,
            qq_listener=QQBotGatewayListener(
                runtime_config=runtime_config,
                bindings=qq_bindings,
                timeout_seconds=settings.http_timeout_seconds,
            ),
            qr_login=BilibiliQrLoginClient(timeout_seconds=settings.http_timeout_seconds),
        )

    def monitor(self) -> MonitorService:
        from yysls_news.collectors.yysls.client import YyslsClient

        return MonitorService(
            credentials=self.credentials,
            subscriptions=self.subscriptions,
            sources=self.sources,
            contents=self.contents,
            yysls_client=YyslsClient(timeout_seconds=self.settings.http_timeout_seconds),
            timeout_seconds=self.settings.http_timeout_seconds,
            credential_lifecycle=(
                BilibiliCredentialLifecycle(self.credentials) if self.credentials else None
            ),
        )
