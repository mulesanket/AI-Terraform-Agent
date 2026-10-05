# tests/conftest.py
import os
import sys
import tempfile
import pytest

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("AGENT_WORKDIR", tempfile.mkdtemp())
os.environ.setdefault("API_KEY", "test-key-123")


@pytest.fixture
def workdir(tmp_path):
    """Create a temporary workdir with a basic main.tf."""
    tf_file = tmp_path / "main.tf"
    tf_file.write_text('resource "null_resource" "test" {}\n')
    return str(tmp_path)


@pytest.fixture
def app():
    """Create Flask test app."""
    os.environ["AGENT_WORKDIR"] = tempfile.mkdtemp()
    os.environ["API_KEY"] = "test-key-123"
    from server import app as flask_app
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def client(app):
    """Flask test client with auth header."""
    return app.test_client()


@pytest.fixture
def auth_headers():
    """Headers with valid API key."""
    return {"Authorization": "Bearer test-key-123", "Content-Type": "application/json"}
