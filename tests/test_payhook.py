import os, sys, importlib, hashlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

def reload_main():
    if 'main' in sys.modules:
        del sys.modules['main']
    return importlib.import_module('main')


def test_payhook_crc(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'key')
    monkeypatch.setenv('ROBOKASSA_PASS2', 'pass2')
    app = reload_main().app
    from fastapi.testclient import TestClient
    client = TestClient(app)
    data = {'InvId': '1', 'OutSum': '199', 'Shp_plan': '15'}
    crc_str = f"{data['OutSum']}:{data['InvId']}:pass2:Shp_plan=15"
    data['SignatureValue'] = hashlib.md5(crc_str.encode()).hexdigest().upper()
    resp = client.post('/payhook', data=data)
    assert resp.json() == 'OK'

    data['SignatureValue'] = 'BAD'
    resp = client.post('/payhook', data=data)
    assert resp.json() == 'bad sign'
