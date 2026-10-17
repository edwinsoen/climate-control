"""Shared provider services for the web process and one-shot worker."""

from dataclasses import dataclass

from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from climate_control.automation import RuleEngine
from climate_control.config import WorkerSettings
from climate_control.db import database
from climate_control.outdoor import OutdoorCollector
from climate_control.polling import Poller
from climate_control.providers.aqicn import AQICNProvider
from climate_control.providers.vesync import VeSyncProvider
from climate_control.providers.xiaomi import XiaomiProvider
from climate_control.security import Vault


@dataclass
class Runtime:
    engine: Engine
    sessions: sessionmaker
    vault: Vault
    outdoor: OutdoorCollector
    poller: Poller


def create_runtime(settings: WorkerSettings, provider=None, outdoor_provider=None):
    engine, sessions = database(settings.database_url)
    vault = Vault(settings.encryption_key)
    outdoor = OutdoorCollector(
        sessions,
        settings,
        outdoor_provider or AQICNProvider(settings.aqicn_api_token.get_secret_value()),
    )
    default_providers = {
        "vesync": VeSyncProvider(settings.request_spacing_seconds),
        "xiaomi": XiaomiProvider(settings.request_spacing_seconds),
    }
    poller = Poller(
        sessions,
        settings,
        vault,
        provider or default_providers,
        outdoor=outdoor,
    )
    poller.automation = RuleEngine(sessions, settings, poller)
    return Runtime(engine, sessions, vault, outdoor, poller)


async def run_tick(settings: WorkerSettings | None = None, provider=None, outdoor_provider=None):
    runtime = create_runtime(settings or WorkerSettings(), provider, outdoor_provider)
    try:
        return await runtime.poller.tick()
    finally:
        runtime.engine.dispose()
