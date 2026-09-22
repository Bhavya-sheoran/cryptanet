#!/usr/bin/env python3
"""Change the Postgres password on a database that already exists.

Postgres sets its password from POSTGRES_PASSWORD only when it first
initialises its data directory. On an existing volume the variable is ignored,
so editing .env alone does not rotate anything - it just makes the backend's
credentials wrong, which looks like an outage rather than a misconfiguration.

This does the other half: `ALTER USER` inside the running database, so the new
value in .env is the one that actually works. Data is untouched.

Both values come from the environment rather than the command line, so neither
appears in shell history or in `ps` output while it runs.

Usage:
    PG_NEW_PASSWORD=... python scripts/rotate_postgres_password.py
"""

from __future__ import annotations

import os
import sys

from sqlalchemy import create_engine, text

from app.config import get_settings


def main() -> int:
    new = os.environ.get("PG_NEW_PASSWORD")
    if not new:
        print("PG_NEW_PASSWORD is not set", file=sys.stderr)
        return 2
    if len(new) < 16:
        print("refusing: PG_NEW_PASSWORD is shorter than 16 characters", file=sys.stderr)
        return 2

    settings = get_settings()
    engine = create_engine(settings.database_url, pool_pre_ping=True)

    with engine.connect() as conn:
        user = conn.execute(text("SELECT current_user")).scalar_one()
        # The password cannot be a bound parameter: ALTER USER takes a literal.
        # quote_literal is applied by the server, so the value is escaped by
        # Postgres itself rather than by string formatting here.
        literal = conn.execute(text("SELECT quote_literal(:p)"), {"p": new}).scalar_one()
        conn.execute(text(f"ALTER USER {user} WITH PASSWORD {literal}"))  # noqa: S608
        conn.commit()

    print(f"password rotated for role '{user}'")
    print("Now set POSTGRES_PASSWORD in .env to the new value and restart the stack.")
    print("Until you do, the backend is still using the old value from its environment.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
