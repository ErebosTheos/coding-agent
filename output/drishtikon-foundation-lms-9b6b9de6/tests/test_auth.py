import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from passlib.context import CryptContext
from src.models.user import User, Role

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

@pytest.mark.asyncio
async def test_user_registration_and_login(client: AsyncClient, db_session: AsyncSession):
    # 1. Create a user manually for testing login
    hashed_pwd = pwd_context.hash("testpassword123")
    user = User(
        email="test@example.com",
        hashed_password=hashed_pwd,
        full_name="Test User",
        role=Role.STUDENT,
        is_active=True
    )
    db_session.add(user)
    await db_session.commit()

    # 2. Attempt login via API
    response = await client.post("/api/v1/auth/login", data={
        "username": "test@example.com",
        "password": "testpassword123"
    })
    
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"

@pytest.mark.asyncio
async def test_invalid_login(client: AsyncClient):
    response = await client.post("/api/v1/auth/login", data={
        "username": "wrong@example.com",
        "password": "wrongpass"
    })
    assert response.status_code == 401