import os
import tempfile
import pytest
from unittest.mock import MagicMock
from codegen_agent.healer import Healer

@pytest.fixture
def temp_workspace():
    """Create a temporary workspace for testing Healer."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        yield tmp_dir

@pytest.fixture
def healer(temp_workspace):
    """Create a Healer instance with a mock LLMClient and a temporary workspace."""
    mock_llm = MagicMock()
    return Healer(llm_client=mock_llm, workspace=temp_workspace)

def test_get_most_recent_file_filters_by_extension(healer, temp_workspace):
    # Create allowed files
    py_file = os.path.join(temp_workspace, "test.py")
    with open(py_file, "w") as f:
        f.write("print('hello')")
    
    # Create disallowed files
    pyc_file = os.path.join(temp_workspace, "test.pyc")
    with open(pyc_file, "wb") as f:
        f.write(b"\x00\x01\x02")
    
    bin_file = os.path.join(temp_workspace, "test.bin")
    with open(bin_file, "wb") as f:
        f.write(b"something binary")
        
    # Set mtime so pyc is "newer"
    os.utime(py_file, (100, 100))
    os.utime(pyc_file, (200, 200))
    os.utime(bin_file, (300, 300))
    
    recent_file = healer._get_most_recent_file()
    
    # It should pick test.py because test.pyc and test.bin are not in ALLOWED_EXTENSIONS
    assert recent_file == "test.py"

def test_extract_target_file_uses_allowed_extensions(healer):
    # Create the file first
    py_path = os.path.join(healer.workspace, "test.py")
    with open(py_path, "w") as f:
        f.write("")
        
    output = 'Error in File "test.py", line 1'
    assert healer._extract_target_file(output) == "test.py"
    
    output_disallowed = 'Error in File "test.pyc", line 1'
    # Even if it exists, it should not be extracted if not in ALLOWED_EXTENSIONS
    pyc_path = os.path.join(healer.workspace, "test.pyc")
    with open(pyc_path, "w") as f:
        f.write("")
        
    assert healer._extract_target_file(output_disallowed) is None
    
    valid_path = os.path.join(healer.workspace, "valid.py")
    with open(valid_path, "w") as f:
        f.write("")
    
    output_valid = 'Error in File "valid.py", line 1'
    assert healer._extract_target_file(output_valid) == "valid.py"


def test_extract_target_file_skips_tests_by_default(healer):
    test_path = os.path.join(healer.workspace, "tests", "test_logic.py")
    os.makedirs(os.path.dirname(test_path), exist_ok=True)
    with open(test_path, "w") as f:
        f.write("")

    output = 'Error in File "tests/test_logic.py", line 1'
    assert healer._extract_target_file(output) is None


def test_extract_target_file_can_edit_tests_when_enabled(temp_workspace):
    mock_llm = MagicMock()
    permissive_healer = Healer(
        llm_client=mock_llm,
        workspace=temp_workspace,
        allow_test_file_edits=True,
    )
    test_path = os.path.join(temp_workspace, "tests", "test_logic.py")
    os.makedirs(os.path.dirname(test_path), exist_ok=True)
    with open(test_path, "w") as f:
        f.write("")

    output = 'Error in File "tests/test_logic.py", line 1'
    assert permissive_healer._extract_target_file(output) == "tests/test_logic.py"
