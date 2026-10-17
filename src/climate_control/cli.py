import argparse
import asyncio
import getpass
import logging

import uvicorn
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet

from climate_control import telemetry
from climate_control.config import AgentSettings, Settings, WorkerSettings
from climate_control.runtime import run_tick
from climate_control.security import password_hasher


def main():
    parser = argparse.ArgumentParser(description="Climate Control administration")
    parser.add_argument(
        "command",
        choices=["key", "hash-password", "migrate", "serve", "serve-api", "tick", "plan"],
    )
    parser.add_argument("--agent-token-hash", default="")
    args = parser.parse_args()
    if args.command == "key":
        print(Fernet.generate_key().decode())
    elif args.command == "hash-password":
        password = getpass.getpass("Administrator password: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm or len(password) < 12:
            parser.error("Passwords must match and contain at least 12 characters.")
        print(password_hasher.hash(password))
    elif args.command == "migrate":
        command.upgrade(Config("alembic.ini"), "head")
        if args.agent_token_hash:
            from climate_control.agent.access import ensure_service_token_hash
            from climate_control.config import DatabaseSettings
            from climate_control.db import database

            engine, sessions = database(DatabaseSettings().database_url)
            try:
                with sessions() as db:
                    ensure_service_token_hash(db, args.agent_token_hash)
            finally:
                engine.dispose()
    elif args.command in {"tick", "plan"}:
        from climate_control.agent.worker import run_plan

        name = "worker_tick" if args.command == "tick" else "planner_tick"
        try:
            settings = WorkerSettings() if args.command == "tick" else AgentSettings()
            telemetry.configure(settings, "worker" if args.command == "tick" else "planner")
            result = asyncio.run(
                run_tick(settings) if args.command == "tick" else run_plan(settings)
            )
        except Exception:
            logging.basicConfig(level=logging.INFO)
            logging.getLogger(__name__).error("%s_failed", name)
            raise SystemExit(1) from None
        logging.getLogger(__name__).info("%s_completed counts=%s", name, result)
    else:
        settings = Settings()
        telemetry.configure(settings, "api" if args.command == "serve-api" else "app")
        if args.command == "serve" and settings.auto_migrate:
            command.upgrade(Config("alembic.ini"), "head")
        uvicorn.run(
            "climate_control.main:create_api_app"
            if args.command == "serve-api"
            else "climate_control.main:create_app",
            factory=True,
            host="0.0.0.0",
            port=settings.port,
            proxy_headers=False,
            access_log=False,
        )


if __name__ == "__main__":
    main()
