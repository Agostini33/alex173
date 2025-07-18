import os, sys, importlib
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))


def reload_main():
    if 'main' in sys.modules:
        del sys.modules['main']
    return importlib.import_module('main')


def test_missing_openai_key(monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    with pytest.raises(ValueError):
        reload_main()


def test_dev_placeholders(monkeypatch, caplog):
    monkeypatch.setenv('OPENAI_API_KEY', 'key')
    monkeypatch.delenv('ROBOKASSA_PASS2', raising=False)
    monkeypatch.delenv('JWT_SECRET', raising=False)
    monkeypatch.setenv('ENV', 'DEV')
    caplog.set_level('WARNING')
    m = reload_main()
    assert m.PASS2 == 'dev-pass2'
    assert len(m.SECRET) >= 32
    assert 'ROBOKASSA_PASS2' in caplog.text
    assert 'JWT_SECRET' in caplog.text


def test_production_no_placeholders(monkeypatch, caplog):
    monkeypatch.setenv('OPENAI_API_KEY', 'key')
    monkeypatch.delenv('ROBOKASSA_PASS2', raising=False)
    monkeypatch.delenv('JWT_SECRET', raising=False)
    monkeypatch.setenv('ENV', 'PRODUCTION')
    caplog.set_level('WARNING')
    m = reload_main()
    assert m.PASS2 is None
    assert m.SECRET is None
    assert 'ROBOKASSA_PASS2' in caplog.text
    assert 'JWT_SECRET' in caplog.text
