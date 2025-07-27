import hashlib
import importlib
import os
import sys

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


def reload_main():
    if "main" in sys.modules:
        del sys.modules["main"]
    return importlib.import_module("main")


def test_payform_crc(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    monkeypatch.setenv("ROBOKASSA_PASS1", "pass1")
    app = reload_main().app
    from fastapi.testclient import TestClient

    client = TestClient(app)

    data = {"plan": "15", "email": "1@1.com", "Shp_extra": "X"}
    resp = client.post("/payform", json=data)
    fields = {
        inp["name"]: inp["value"]
        for inp in BeautifulSoup(resp.json()["form"], "html.parser").find_all("input")
    }

    shp_params = {k: fields[k] for k in fields if k.startswith("Shp_")}
    shp_part = ":".join(f"{k}={shp_params[k]}" for k in sorted(shp_params))
    crc_str = f"{fields['MerchantLogin']}:{fields['OutSum']}:{fields['InvId']}:pass1"
    if shp_part:
        crc_str += f":{shp_part}"
    assert fields["SignatureValue"] == hashlib.md5(crc_str.encode()).hexdigest()
    assert "Desc" not in fields
    assert fields["Email"] == "1@1.com"
    assert "Shp_plan" not in fields
