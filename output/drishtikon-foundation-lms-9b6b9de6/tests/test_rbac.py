import pytest
from httpx import AsyncClient
from src.models.user import Role, User
from src.services.auth_service import AuthService

@pytest.mark.asyncio
async def test_admin_access_to_stats(client: AsyncClient, db_session):
    # Create Admin
    admin_pwd = AuthService.hash_password("admin123")
    admin = User(email="admin@lms.com", hashed_password=admin_pwd, role=Role.ADMIN, is_active=True)
    db_session.add(admin)
    await db_session.commit()

    # Login
    login_res = await client.post("/api/v1/auth/auth/login", data={"username": "admin@lms.com", "password": "admin123"})
    token = login_res.json()["access_token"]

    # Access admin endpoint
    headers = {"Authorization": f"Bearer {token}"}
    res = await client.get("/api/v1/admin/stats", headers=headers)
    assert res.status_code == 200

@pytest.mark.asyncio
async def test_student_denied_admin_access(client: AsyncClient, db_session):
    # Create Student
    std_pwd = AuthService.hash_password("std123")
    student = User(email="student@lms.com", hashed_password=std_pwd, role=Role.STUDENT, is_active=True)
    db_session.add(student)
    await db_session.commit()

    # Login
    login_res = await client.post("/api/v1/auth/auth/login", data={"username": "student@lms.com", "password": "std123"})
    token = login_res.json()["access_token"]

    # Access admin endpoint
    headers = {"Authorization": f"Bearer {token}"}
    res = await client.get("/api/v1/admin/stats", headers=headers)
    assert res.status_code == 403