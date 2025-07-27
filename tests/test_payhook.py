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
    data = {'InvId': '1', 'OutSum': '199'}
    shp_params = {k: data[k] for k in data if k.startswith('Shp_')}
    shp_part = ':'.join(f"{k}={shp_params[k]}" for k in sorted(shp_params))
    crc_str = f"{data['OutSum']}:{data['InvId']}:pass2"
    if shp_part:
        crc_str += f":{shp_part}"
    data['SignatureValue'] = hashlib.md5(crc_str.encode()).hexdigest().upper()
    resp = client.post('/payhook', data=data)
    assert resp.json() == 'OK'

    data['SignatureValue'] = 'BAD'
    resp = client.post('/payhook', data=data)
    assert resp.json() == 'bad sign'


def test_paytoken_persistence(monkeypatch, tmp_path):
    monkeypatch.setenv('OPENAI_API_KEY', 'key')
    monkeypatch.setenv('ROBOKASSA_PASS2', 'pass2')
    monkeypatch.setenv('TOKENS_DB', str(tmp_path / 'tok.db'))

    m = reload_main()
    from fastapi.testclient import TestClient
    client = TestClient(m.app)
    data = {'InvId': '42', 'OutSum': '199'}
    shp_params = {k: data[k] for k in data if k.startswith('Shp_')}
    shp_part = ':'.join(f"{k}={shp_params[k]}" for k in sorted(shp_params))
    crc_str = f"{data['OutSum']}:{data['InvId']}:pass2"
    if shp_part:
        crc_str += f":{shp_part}"
    data['SignatureValue'] = hashlib.md5(crc_str.encode()).hexdigest().upper()
    client.post('/payhook', data=data)

    m2 = reload_main()
    client2 = TestClient(m2.app)
    resp = client2.get('/paytoken', params={'inv': data['InvId']})
    assert 'token' in resp.json()
    resp2 = client2.get('/paytoken', params={'inv': data['InvId']})
    assert resp2.json()['error'] == 'NOT_READY'


def test_payhook_quota_one(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'key')
    monkeypatch.setenv('ROBOKASSA_PASS2', 'pass2')

    m = reload_main()
    from fastapi.testclient import TestClient
    client = TestClient(m.app)
    data = {'InvId': '7', 'OutSum': '1.00'}
    shp_params = {k: data[k] for k in data if k.startswith('Shp_')}
    shp_part = ':'.join(f"{k}={shp_params[k]}" for k in sorted(shp_params))
    crc_str = f"{data['OutSum']}:{data['InvId']}:pass2"
    if shp_part:
        crc_str += f":{shp_part}"
    data['SignatureValue'] = hashlib.md5(crc_str.encode()).hexdigest().upper()
    resp = client.post('/payhook', data=data)
    assert resp.json() == 'OK'
    assert m.ACCOUNTS['user@wb6']['quota'] == 1
