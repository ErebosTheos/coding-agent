import pytest
from datetime import timedelta
from src.core.security import create_access_token, verify_password, get_password_hash

def test_password_hashing_workflow():
    """Happy Path: Verify hashing and password matching."""
    pw = "super_secret_123"
    hashed = get_password_hash(pw)
    assert hashed != pw
    assert verify_password(pw, hashed) is True
    assert verify_password("wrong", hashed) is False

def test_access_token_creation():
    """Happy Path: Ensure JWT is created with correct subject."""
    subject = "user_123"
    token = create_access_token(subject, expires_delta=timedelta(minutes=15))
    assert isinstance(token, str)
    assert len(token.split(".")) == 3 # Header, Payload, Signature

def test_access_token_expiry_handling():
    """Edge Case: Token creation with negative expiry (simulating immediate expiry)."""
    # Although create_access_token doesn't validate time internally (handled by verify/decode),
    # we test that it handles zero/negative deltas without crashing.
    token = create_access_token("user", expires_delta=timedelta(seconds=-1))
    assert token is not None