import pytest
from sqlalchemy.exc import IntegrityError
from src.models.user import User, Role, StudentProfile, TeacherProfile

@pytest.mark.asyncio
async def test_create_user_with_profiles(db):
    """Happy Path: Create a user and link multiple role-specific profiles."""
    user = User(email="test_user@example.com", hashed_password="hash123", role=Role.STUDENT, is_active=True)
    db.add(user)
    await db.commit()
    await db.refresh(user)

    profile = StudentProfile(user_id=user.id, disability_type="Visual Impairment")
    db.add(profile)
    await db.commit()

    assert user.id is not None
    assert user.role == Role.STUDENT
    assert profile.user_id == user.id

@pytest.mark.asyncio
async def test_duplicate_email_integrity(db):
    """Edge Case: Ensure unique constraint on email is enforced at the DB level."""
    u1 = User(email="unique@example.com", hashed_password="p", role=Role.STUDENT)
    db.add(u1)
    await db.commit()

    u2 = User(email="unique@example.com", hashed_password="p", role=Role.TEACHER)
    db.add(u2)
    with pytest.raises(IntegrityError):
        await db.commit()

@pytest.mark.asyncio
async def test_role_enum_assignment(db):
    """Edge Case: Test role assignment with Enum values and validation."""
    user = User(email="staff@example.com", hashed_password="p", role=Role.STAFF)
    db.add(user)
    await db.commit()
    assert user.role == "staff" # Enum string value
    assert isinstance(user.role, Role)