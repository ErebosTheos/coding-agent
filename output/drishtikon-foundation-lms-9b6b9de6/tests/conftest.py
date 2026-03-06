import os
import pytest
import asyncio
from typing import AsyncGenerator

# MANDATORY: Set env vars before any local imports to prevent config initialization errors
os.environ.setdefault("SECRET_KEY", "production_quality_test_secret_key_67890")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("ENVIRONMENT", "testing")

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from httpx import AsyncClient, ASGITransport
from src.main import app
from src.db.base_class import Base
from src.db.session import get_db

# SQLite in-memory for fast unit testing
DATABASE_URL = "sqlite+aiosqlite:///:memory:"
engine = create_async_engine(DATABASE_URL, echo=False)
TestingSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for each test case."""
    policy = asyncio.get_event_loop_policy()
    loop = policy.new_event_loop()
    yield loop
    loop.close()

@pytest.fixture(scope="function", autouse=True)
async def setup_db():
    """Initialize and clear the database for every test to ensure isolation."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

@pytest.fixture
async def db() -> AsyncGenerator[AsyncSession, None]:
    """Provides an async session for database operations within tests."""
    async with TestingSessionLocal() as session:
        yield session

@pytest.fixture
async def client(db) -> AsyncGenerator[AsyncClient, None]:
    """Provides an AsyncClient with the database dependency overridden."""
    async def override_get_db():
        try:
            yield db
        finally:
            pass
    
    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()