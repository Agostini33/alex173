import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
# Ensure required env vars for import
os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("ENV", "DEV")
from fastapi.testclient import TestClient
from main import app
import pytest

client = TestClient(app)

@pytest.mark.parametrize(
    "origin",
    [
        "http://wb6.ru",
        "https://www.wb6.ru",
        "http://www.wb6.ru",
    ],
)
def test_wb6_origins_allowed(origin):
    resp = client.options(
        "/rewrite",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == origin

def test_env_cors_origin():
    os.environ["CORS_ORIGINS"] = "http://example.com"
    import importlib
    import main
    importlib.reload(main)
    cli = TestClient(main.app)
    resp = cli.options(
        "/rewrite",
        headers={
            "Origin": "http://example.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "http://example.com"
