"""Create an organisation with one API key per role. Each key is printed once, never stored.

    docker compose run --rm tools python -m scripts.seed --name "Demo Hospital Network"

Runs as the schema owner. FORCE ROW LEVEL SECURITY applies to the owner too, so the new
org's id is set for the transaction before its rows are written.
"""

import argparse
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import build_engine, set_org
from app.models.tables import ApiKey, Org
from app.services.auth import ROLES, Role, generate_api_key


def create_org(name: str, roles: tuple[Role, ...] = ROLES) -> tuple[UUID, dict[str, str]]:
    settings = get_settings()
    pepper = settings.api_key_pepper.get_secret_value()
    engine = build_engine(settings.migration_database_url, pool_size=1)
    org_id = uuid4()
    keys: dict[str, str] = {}
    try:
        with Session(engine) as session, session.begin():
            set_org(session, org_id)
            session.add(Org(id=org_id, name=name))
            session.flush()  # the models declare no relationships, so insert the parent first
            for role in roles:
                new_key = generate_api_key(pepper)
                session.add(
                    ApiKey(
                        id=uuid4(),
                        org_id=org_id,
                        name=f"{name} ({role})",
                        key_prefix=new_key.prefix,
                        key_hash=new_key.key_hash,
                        role=role,
                    )
                )
                keys[role] = new_key.key
    finally:
        engine.dispose()
    return org_id, keys


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name", default="Demo Hospital Network")
    parser.add_argument(
        "--with-other-org",
        action="store_true",
        help="also create a second org and print its reader key (the demo's isolation check)",
    )
    args = parser.parse_args()
    org_id, keys = create_org(args.name)
    print(f"# Organisation {args.name!r} ({org_id}). Keys are shown once; store them now.")
    for role, key in keys.items():
        print(f"MEDVORA_{role.upper()}_KEY={key}")
    if args.with_other_org:
        _, other_keys = create_org(f"{args.name} (second org)")
        print(f"MEDVORA_OTHER_ORG_READER_KEY={other_keys['reader']}")


if __name__ == "__main__":
    main()
