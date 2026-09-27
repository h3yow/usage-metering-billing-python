"""pytest 公共夹具：每次测试一个全新 store（已写目录）+ FastAPI TestClient。"""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app.bootstrap import create_app
from app.storage import LocalStore

logging.basicConfig(level=logging.INFO)


@pytest.fixture
def store() -> LocalStore:
    s = LocalStore()
    from app.bootstrap import seed_catalog
    seed_catalog(s)
    return s


@pytest.fixture
def client(store) -> TestClient:
    app = create_app(store, seed=False)
    return TestClient(app)
