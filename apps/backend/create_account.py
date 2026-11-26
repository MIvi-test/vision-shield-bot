"""Explicit panel account provisioning; password is read without terminal echo."""

import argparse
import getpass
from uuid import uuid4

from backend.database import PanelAccount, database_engine
from backend.passwords import password_hash
from sqlalchemy import select
from sqlalchemy.orm import Session


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", required=True)
    parser.add_argument(
        "--role", choices=["moderator", "organizer", "admin"], default="moderator"
    )
    args = parser.parse_args()
    if not 1 <= len(args.username) <= 64:
        parser.error("Имя: 1–64 символа")
    password = getpass.getpass("Пароль: ")
    if len(password) < 12 or password != getpass.getpass("Повторите пароль: "):
        parser.error("Нужен совпадающий пароль не короче 12 символов")
    engine = database_engine()
    try:
        with Session(engine) as session, session.begin():
            if (
                session.scalar(
                    select(PanelAccount.id).where(
                        PanelAccount.username == args.username
                    )
                )
                is not None
            ):
                parser.error("Аккаунт уже существует; пароль не изменён")
            session.add(
                PanelAccount(
                    id=uuid4(),
                    username=args.username,
                    role=args.role,
                    password_hash=password_hash(password),
                    active=True,
                )
            )
        print("Аккаунт создан.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
