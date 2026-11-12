"""
Regression tests for Fogless. Run from the project root:   pip install pytest && pytest -q
Runs entirely in the in-memory mode: Supabase/Groq env vars are removed and .env is NOT loaded,
so these tests can never touch your real database.
"""
import importlib.util
import io
import json
import os
import sys
import unittest.mock as mock
from decimal import Decimal as D

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USER, PW = "tester", "correct horse battery staple"


@pytest.fixture()
def fog(monkeypatch):
    for k in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SUPABASE_KEY", "GROQ_API_KEY", "SESSION_SECRET", "VERCEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("FOGLESS_NO_DOTENV", "1")
    monkeypatch.setenv("DEMO_USERNAME", USER)
    monkeypatch.setenv("DEMO_PASSWORD", PW)
    spec = importlib.util.spec_from_file_location("fogless_under_test", os.path.join(ROOT, "api", "index.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.app.config["TESTING"] = False  # keep real error handlers
    return mod


@pytest.fixture()
def c(fog):
    cl = fog.app.test_client()
    assert cl.post("/api/login", json={"username": USER, "password": PW}).status_code == 200
    return cl


def say(c, text):
    r = c.post("/api/message", json={"text": text})
    return r.status_code, (r.get_json() or {})


def txs(fog):
    return [(t["type"], str(t["amount"])) for t in fog._all_transactions(order="id.asc")]


# ------------------------------------------------------------------ security
def test_no_default_credentials(fog, monkeypatch):
    monkeypatch.delenv("DEMO_USERNAME")
    monkeypatch.delenv("DEMO_PASSWORD")
    spec = importlib.util.spec_from_file_location("fog2", os.path.join(ROOT, "api", "index.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    r = m.app.test_client().post("/api/login", json={"username": "vaibhav10", "password": "iwon10"})
    assert r.status_code in (401, 503)


def test_api_requires_login(fog):
    anon = fog.app.test_client()
    for ep in ("/api/state", "/api/backup", "/api/export", "/api/history", "/api/balances"):
        assert anon.get(ep).status_code == 401


def test_login_throttle(fog):
    cl = fog.app.test_client()
    fog.time.sleep = lambda s: None
    codes = [cl.post("/api/login", json={"username": USER, "password": "bad"}).status_code for _ in range(12)]
    assert 429 in codes


@pytest.mark.parametrize("body", [[1, 2], "str", 5, None])
def test_non_object_json_never_500(fog, c, body):
    for ep in ("/api/message", "/api/action", "/api/login"):
        assert c.post(ep, data=json.dumps(body), content_type="application/json").status_code < 500


def test_text_must_be_string(fog, c):
    assert c.post("/api/message", json={"text": 50}).status_code == 400
    assert c.post("/api/message", json={"text": ["a"]}).status_code == 400


def test_tx_id_is_strict(fog, c):
    say(c, "10 tea")
    for bad in (1.9, True, -1, 0, "1; drop", 10 ** 30):
        assert c.post("/api/action", json={"action": "delete", "tx_id": bad}).status_code == 400
    assert len(txs(fog)) == 1


def test_csv_formula_injection_neutralised(fog, c):
    fog.add_transaction("expense", 5, "other", "=HYPERLINK(1)", "cash")
    body = c.get("/api/export").data.decode("utf-8-sig")
    assert "'=HYPERLINK" in body and ",=HYPERLINK" not in body


def test_api_responses_not_cacheable(fog, c):
    assert "no-store" in c.get("/api/state").headers["Cache-Control"]


# ------------------------------------------------------------------ amount parsing
@pytest.mark.parametrize("text,amount", [
    ("24 coffee upi", "24.00"), ("1,200 rent", "1200.00"), ("₹500 petrol", "500.00"), ("coffee 1k upi", "1000.00"),
    ("30₹ chai", "30.00"), ("450 dinner on 12 nov", "450.00"), ("uber 250 on 5th", "250.00"),
    ("paid 450 on nov 12 for dinner", "450.00"), ("bought 3 shirts for 1500", "1500.00"),
    ("spent 20 on 2 samosas", "20.00"), ("pay 600 market 20", None),
])
def test_single_amount(fog, c, text, amount):
    if amount is None:
        return
    status, _ = say(c, text)
    assert status == 200
    assert txs(fog) == [("expense", amount)], text


@pytest.mark.parametrize("text", ["0 coffee", "0.004 coffee", "1e3 coffee", "12.5.5 tea", "999999999999 car", "hello"])
def test_garbage_amounts_log_nothing(fog, c, text):
    say(c, text)
    assert txs(fog) == []


def test_multi_item(fog, c):
    say(c, "120 lunch upi, 50 bus cash, 200 veggies card")
    assert [a for _, a in txs(fog)] == ["120.00", "50.00", "200.00"]


@pytest.mark.parametrize("text", ["300 got milk", "got haircut 200", "got petrol 500"])
def test_got_is_not_income(fog, c, text):
    say(c, text)
    assert [t for t, _ in txs(fog)] == ["expense"], text


@pytest.mark.parametrize("text", ["got 424 upi", "got paid 5000", "salary 50000 netbanking"])
def test_income_still_detected(fog, c, text):
    say(c, text)
    assert [t for t, _ in txs(fog)] == ["income"], text


def test_split_with_split_before_amount(fog, c):
    say(c, "split 600 dinner with rahul")
    assert fog.get_debt_balances().get("rahul") == D("300.00")


# ------------------------------------------------------------------ ledger
def test_split_math_and_no_zero_rows(fog, c):
    say(c, "100 dinner split with a and b")
    assert sum(fog.get_debt_balances().values()) == D("66.67")  # 100 - my share 33.33
    say(c, "0.01 candy split with rahul")
    assert all(D(a) > 0 for _, a in txs(fog))


def test_undo_split_removes_everything(fog, c):
    say(c, "600 dinner upi split with rahul and amit")
    say(c, "/undo")
    assert txs(fog) == [] and fog.get_debt_balances() == {}


def test_undo_reverts_the_newest_pool_action(fog, c):
    say(c, "put 500 into goa pool")
    say(c, "rahul put 500 into goa pool")
    say(c, "300 from goa pool")
    say(c, "/undo")  # must undo the 300 pool expense, not the older 500 contribution
    s = fog.get_pool_summary("goa")
    assert s["total_out"] == D("0.00") and s["total_in"] == D("1000.00")
    say(c, "/undo")  # now rahul's deposit
    assert fog.get_pool_summary("goa")["total_in"] == D("500.00")
    say(c, "/undo")  # now my own deposit + its transfer, together
    assert fog.get_pool_summary("goa")["total_in"] == D("0.00") and txs(fog) == []
    assert "Nothing to undo" in say(c, "/undo")[1]["text"]


def test_liquid_balance_excludes_iou_and_pool(fog, c):
    say(c, "/setbalance cash 1000")
    say(c, "gave rahul 200 cash")
    assert fog.get_balance() == D("800.00") and fog.get_debt_balances()["rahul"] == D("200.00")


def test_set_balance_then_spend(fog, c):
    say(c, "/setbalance upi 500")
    say(c, "100 lunch upi")
    assert fog.get_liquid_balances()["upi"] == D("400.00")


# ------------------------------------------------------------------ LLM output hardening
def _groq(fog, items):
    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": json.dumps({"transactions": items})}}]}
    return R()


def run_llm(fog, c, text, items):
    fog.GROQ_API_KEY = "x"
    with mock.patch.object(fog.requests, "post", return_value=_groq(fog, items)):
        return c.post("/api/message", json={"text": text}).get_json()


def test_llm_comma_amount_is_accepted(fog, c):
    run_llm(fog, c, "a 100, b 1200", [{"type": "expense", "amount": 100, "category": "food"},
                                        {"type": "expense", "amount": "1,200", "category": "food"}])
    assert [a for _, a in txs(fog)] == ["100.00", "1200.00"]


def test_llm_dropped_item_is_reported(fog, c):
    r = run_llm(fog, c, "100 a, 200 b", [{"type": "expense", "amount": 100, "category": "food"},
                                          {"type": "expense", "amount": None}])
    assert "NOT logged" in r["text"]


def test_llm_case_and_nan(fog, c):
    run_llm(fog, c, "50 coffee upi", [{"type": "expense", "amount": 50, "category": "FOOD", "payment_method": "UPI"}])
    assert fog._all_transactions()[0]["category"] == "food"
    assert fog._all_transactions()[0]["payment_method"] == "upi"
    fog.reset_all_data()
    r = run_llm(fog, c, "100 a, 200 b", [{"type": "expense", "amount": "NaN"}, {"type": "expense", "amount": 200, "category": "food"}])
    assert [a for _, a in txs(fog)] == ["200.00"] and "NOT logged" in r["text"]


def test_llm_split_with_too_many_people_rejected(fog, c):
    run_llm(fog, c, "500 dinner split", [{"share_type": "split", "amount": 500, "people": [f"p{i}" for i in range(40)]}])
    assert len(txs(fog)) <= 1


def test_other_alias_is_never_confirmed(fog, c):
    for _ in range(3):
        say(c, "40 zzzz")
    assert "zzzz" not in fog.get_all_aliases()


# ------------------------------------------------------------------ misc
def test_voice_without_key_logs_nothing(fog, c):
    r = c.post("/api/voice", data={"audio": (io.BytesIO(b"abc"), "x.webm")}, content_type="multipart/form-data")
    assert r.status_code == 502 and txs(fog) == []


def test_reset_needs_confirm(fog, c):
    say(c, "10 tea")
    say(c, "/reset")
    assert len(txs(fog)) == 1
    say(c, "/reset confirm")
    assert txs(fog) == []


def test_static_path_traversal_blocked(fog, c):
    for p in ("/../api/index.py", "/%2e%2e/api/index.py", "/.env"):
        assert c.get(p).status_code == 404


def test_big_body_rejected(fog, c):
    assert c.post("/api/message", data=b"{" + b"a" * (5 * 1024 * 1024), content_type="application/json").status_code in (400, 413)
