"""Explicit local campaign setup; rules are never invented by the bot."""

import argparse
from pathlib import Path

from backend.database import database_engine
from backend.submissions import register_campaign
from sqlalchemy.orm import Session


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Создать кампанию или новую версию правил"
    )
    parser.add_argument("--slug", required=True)
    parser.add_argument("--rules-version", required=True)
    parser.add_argument("--rules-file", required=True, type=Path)
    args = parser.parse_args()
    engine = database_engine()
    try:
        rules = args.rules_file.read_text(encoding="utf-8").strip()
        with Session(engine) as session, session.begin():
            campaign_id = register_campaign(
                session, args.slug, args.rules_version, rules
            )
        print(f"BACKEND_CAMPAIGN_ID={campaign_id}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
