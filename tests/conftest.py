"""Point the whole test session at its own database before any settings are read.

Integration tests truncate every table, so they must never run against the database the
app (and its worker) use. deploy/postgres/init-roles.sh creates `<name>_test` for this.
"""

import os

os.environ["DB_NAME"] = os.environ.get("TEST_DB_NAME", "medvora_test")
