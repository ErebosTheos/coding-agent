import pytest
from httpx import AsyncClient
from src.models.interaction import Assessment, Submission
from src.models.user import User, Role
from src.core.deps import get_current_user
from src.main import app

@pytest.mark.asyncio
async def test_create_assessment_authorized(client: AsyncClient, db):
    """Happy Path: Teacher successfully creates a timed assessment."""
    # Mock Teacher user
    async def mock_teacher():
        return User(id=10, email="teacher@lms.org", role=Role.TEACHER, is_active=True)
    app.dependency_overrides[get_current_user] = mock_teacher

    assessment_data = {
        "title": "Weekly Mobility Test",
        "description": "Test on cane techniques",
        "duration_minutes": 45,
        "assessment_type": "mcq"
    }
    response = await client.post("/api/v1/assessments/", json=assessment_data)
    assert response.status_code == 200
    assert response.json()["title"] == "Weekly Mobility Test"
    app.dependency_overrides.clear()

@pytest.mark.asyncio
async def test_submit_assessment_after_deadline(client: AsyncClient, db):
    """Edge Case: Student submits an assessment after the timer has theoretically expired."""
    assessment = Assessment(title="Late Quiz", duration_minutes=0) # 0 minutes to force expiry
    db.add(assessment)
    await db.commit()

    response = await client.post(f"/api/v1/assessments/{assessment.id}/submit", json={"answers": []})
    # Depending on implementation, this should be 400 or 403
    assert response.status_code in [400, 403]

@pytest.mark.asyncio
async def test_grade_submission_out_of_bounds(client: AsyncClient, db):
    """Edge Case: Admin/Teacher assigns a score higher than allowed (e.g., > 100)."""
    submission = Submission(assessment_id=1, student_id=1, status="submitted")
    db.add(submission)
    await db.commit()

    # Test score 101 on a 100 point scale
    response = await client.patch(f"/api/v1/assessments/submissions/{submission.id}/grade", json={"score": 101, "feedback": "Exceeds max"})
    assert response.status_code == 422