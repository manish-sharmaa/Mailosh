"""The one `DeclarativeBase` every model in `mailosh.db.models` inherits
from — kept in its own module (rather than defined inline in `models.py`)
specifically so `tests/conftest.py`'s `db` fixture can depend on just
`Base.metadata` without importing `models` itself. That works only because,
by the time any test actually runs, `models` has *already* been imported
(every test module that uses the `db` fixture also does
`from mailosh.db import models` or `from mailosh.db import models, repo`
at module scope — see `tests/unit/test_db_models.py` — and Python imports
run at collection time, before any fixture executes), which is what
registers every table on this shared `Base.metadata` in the first place.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared declarative base — one `MetaData` for every Mailosh table."""
