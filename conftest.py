"""Point the test suite at a scratch PostgreSQL database.

The fixtures drop and recreate every table in the database they use, so tests
must never inherit RELAY_DATABASE_URL / DATABASE_URL from your shell (which
could be the real relay database).  Use RELAY_TEST_DATABASE_URL to choose a
different scratch server; the default is the relay_test database that
``docker compose up -d postgres`` creates.
"""

import os

TEST_DATABASE_URL = os.getenv(
    "RELAY_TEST_DATABASE_URL", "postgresql+psycopg://relay:relay@127.0.0.1:5434/relay_test"
)
os.environ["RELAY_DATABASE_URL"] = TEST_DATABASE_URL
os.environ.pop("DATABASE_URL", None)
