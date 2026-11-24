import os
import tempfile
from pathlib import Path

import pytest

_DB_PATH = Path(tempfile.gettempdir()) / "gsb_unit_catalog_pytest.db"
for suffix in ("", "-wal", "-shm"):
    path = Path(str(_DB_PATH) + suffix)
    if path.exists():
        path.unlink()

# Must be set before importing app.database.
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with TestClient(app) as test_client:
        yield test_client
