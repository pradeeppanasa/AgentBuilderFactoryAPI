"""Bootstrap script: create the default admin user if one doesn't already exist.

Run from the host venv (scripts/local-setup.sh invokes this as
`python -m scripts.create_admin` — module mode, not `python
scripts/create_admin.py` path mode, since only module mode puts the repo
root on sys.path and lets `from app.config import settings` resolve;
verified empirically, see docs/local-dev.md). app.config.settings reads
.env itself via pydantic-settings, which is what makes database_url
resolve to the host-mapped Postgres port (5433) rather than the in-network
"postgres" hostname the container itself uses — no shell exporting needed.
DEFAULT_ADMIN_EMAIL/PASSWORD/TENANT_ID are bootstrap-only values, read via
scripts/_dotenv.py since they're deliberately not app.config.Settings
fields (nothing at runtime ever reads them again after this script runs).

Idempotent — looks the user up by email first; does nothing if found.
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from app.config import settings
from app.modules.auth.db import create_db_engine, create_session_factory
from app.modules.auth.models import User
from app.modules.auth.security import hash_password
from scripts._dotenv import load_env


async def main() -> None:
    env = load_env()
    email = env.get("DEFAULT_ADMIN_EMAIL")
    password = env.get("DEFAULT_ADMIN_PASSWORD")
    tenant_id = env.get("DEFAULT_ADMIN_TENANT_ID", "default")

    if not email or not password:
        print(
            "DEFAULT_ADMIN_EMAIL / DEFAULT_ADMIN_PASSWORD not set — skipping admin user creation.",
            file=sys.stderr,
        )
        return

    engine = create_db_engine(settings)
    session_factory = create_session_factory(engine)

    try:
        async with session_factory() as session:
            existing = await session.scalar(select(User).where(User.email == email))
            if existing is not None:
                print(f"Admin user {email!r} already exists — skipping.")
                return

            session.add(
                User(
                    email=email,
                    hashed_password=hash_password(password),
                    role="admin",
                    tenant_id=tenant_id,
                    is_active=True,
                )
            )
            await session.commit()
            print(f"Created admin user {email!r} (tenant_id={tenant_id!r}).")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
