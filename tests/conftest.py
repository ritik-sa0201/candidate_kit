import os

import pytest

# Never run tests against the dev database.
os.environ["MONGO_DB"] = os.getenv("TEST_MONGO_DB", "attendance_test")

if os.getenv("USE_MONGOMOCK") == "1":
    import mongomock
    import pymongo

    pymongo.MongoClient = mongomock.MongoClient

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402


@pytest.fixture
def api():
    main.employees.delete_many({})
    main.logs.delete_many({})
    with TestClient(main.app) as c:  # runs the lifespan, so indexes get created
        yield c


@pytest.fixture
def db():
    return main.db
