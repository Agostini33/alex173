import os, re
import pytest

PAY_HTML = os.path.join(os.path.dirname(__file__), '..', 'frontend', 'pay.html')


def test_pay_form_replaced():
    html = open(PAY_HTML, encoding='utf-8').read()
    # Ensure previous RK form is removed before inserting a new one
    assert re.search(r"document.getElementById\('rk'\).*remove()", html, re.S)
