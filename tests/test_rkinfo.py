import os, sys, importlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))


def reload_main():
    if 'main' in sys.modules:
        del sys.modules['main']
    return importlib.import_module('main')


def test_rkinfo(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'key')
    monkeypatch.setenv('ROBOKASSA_PASS1', 'p1')
    monkeypatch.setenv('ROBOKASSA_PASS2', 'p2')
    app = reload_main().app
    from fastapi.testclient import TestClient
    client = TestClient(app)
    resp = client.get('/rkinfo')
    js = resp.json()
    assert js['pass1'] == 'p1'
    assert js['pass2'] == 'p2'
    assert 'crc_formula' in js
