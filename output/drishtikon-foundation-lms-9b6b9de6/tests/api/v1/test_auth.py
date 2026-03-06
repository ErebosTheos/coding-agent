import pytest
from httpx import AsyncClient
from src.core.security import get_password_hash
from src.models.user import User, Role

@pytest.mark.asyncio
async def test_login_flow_success(client: AsyncClient, db):
    """Happy Path: Verify successful login returns JWT tokens."""
    password = "correct_password"
    hashed = get_password_hash(password)
    user = User(email="auth_test@example.com", hashed_password=hashed, role=Role.ADMIN, is_active=True)
    db.add(user)
    await db.commit()

    login_data = {"username": "auth_test@example.com", "password": password}
    response = await client.post("/api/v1/auth/auth/login", data=login_data)
    
    assert response.status_code == 200
    json_data = response.json()
    assert "access_token" in json_data
    assert json_data["token_type"] == "bearer"

@pytest.mark.asyncio
async def test_login_invalid_password(client: AsyncClient, db):
    """Edge Case: Login attempt with incorrect password."""
    user = User(email="wrong_pass@example.com", hashed_password=get_password_hash("right"), role=Role.STUDENT)
    db.add(user)
    await db.commit()

    response = await client.post("/api/v1/auth/auth/login", data={"username": "wrong_pass@example.com", "password": "wrong"})
    assert response.status_code == 401
    assert response.json()["detail"] == "Incorrect email or password"

@pytest.mark.asyncio
async def test_refresh_token_revoked_or_invalid(client: AsyncClient):
    """Edge Case: Refreshing with a malformed or expired token."""
    response = await client.post("/api/v1/auth/auth/refresh", json={"refresh_token": "invalid_payload"})
    assert response.status_code == 401