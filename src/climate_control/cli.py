import argparse
import asyncio
import getpass
import logging
import os

import uvicorn
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet

from climate_control.runtime import run_tick
from climate_control.security import password_hasher


def main():
    parser = argparse.ArgumentParser(description="Climate Control administration")
    parser.add_argument(
        "command", choices=["key", "hash-password", "migrate", "serve", "serve-api", "tick"]
    )
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
    elif args.command == "tick":
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
        try:
            result = asyncio.run(run_tick())
        except Exception:
            logging.getLogger(__name__).error("worker_tick_failed")
            raise SystemExit(1) from None
        logging.getLogger(__name__).info("worker_tick_completed counts=%s", result)
    else:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
        if args.command == "serve" and os.environ.get("AUTO_MIGRATE", "false").lower() == "true":
            command.upgrade(Config("alembic.ini"), "head")
        uvicorn.run(
            "climate_control.main:create_api_app"
            if args.command == "serve-api"
            else "climate_control.main:create_app",
            factory=True,
            host="0.0.0.0",
            port=int(os.environ.get("PORT", "8080")),
            proxy_headers=False,
            access_log=False,
        )


if __name__ == "__main__":
    main()
