import argparse
import getpass
import logging
import os

import uvicorn
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet

from climate_control.security import password_hasher


def main():
    parser = argparse.ArgumentParser(description="Climate Control administration")
    parser.add_argument("command", choices=["key", "hash-password", "migrate", "serve"])
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
    else:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
        if os.environ.get("AUTO_MIGRATE", "false").lower() == "true":
            command.upgrade(Config("alembic.ini"), "head")
        uvicorn.run(
            "climate_control.main:create_app",
            factory=True,
            host="0.0.0.0",
            port=int(os.environ.get("PORT", "8080")),
            proxy_headers=False,
            access_log=False,
        )


if __name__ == "__main__":
    main()
