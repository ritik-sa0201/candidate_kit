import os

import pytest

os.environ["MONGO_DB"] = os.getenv("TEST_MONGO_DB", "attendance_test")

if os.getenv("USE_MONGOMOCK") == "1":
    import mongomock
    import pymongo

    pymongo.MongoClient = mongomock.MongoClient
from fastapi.testclient import TestClient 
from app import main
@pytest.fixture
def api():
    main.employees.delete_many({})
    main.logs.delete_many({})
    with TestClient(main.app) as c:
        yield c
@pytest.fixture
def db():
    return main.db
