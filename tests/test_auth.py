"""Username/password accounts for the website. No model calls."""
import uuid

import pytest
from fastapi.testclient import TestClient

from app import ads, api, security
from app.db import query

client = TestClient(api.app)


def uname():
    return "u" + uuid.uuid4().hex[:10]


@pytest.fixture(autouse=True)
def fresh_limiter(monkeypatch):
    monkeypatch.setattr(api, "auth_limiter", security.RateLimiter(limit=1000, window_s=60))


def signup(username, password="correct horse 9", name="Crumbs Bakery"):
    return client.post("/api/signup", json={"username": username, "password": password, "business_name": name})


def login(username, password="correct horse 9"):
    return client.post("/api/login", json={"username": username, "password": password})


def test_signup_then_login_returns_a_working_session():
    u = uname()
    r = signup(u)
    assert r.status_code == 200 and r.json()["user_id"] == f"acct_{u}" and r.json()["business_name"] == "Crumbs Bakery"
    r = login(u)
    assert r.status_code == 200
    me = client.get("/api/me/campaigns", headers={"Authorization": f"Bearer {r.json()['token']}"})
    assert me.status_code == 200


def test_username_is_case_insensitive():
    u = uname()
    signup(u)
    assert login(u.upper()).status_code == 200


def test_duplicate_username_rejected():
    u = uname()
    assert signup(u).status_code == 200
    r = signup(u, password="another pass 1")
    assert r.status_code == 409 and "taken" in r.json()["detail"]


@pytest.mark.parametrize("username, password, name, msg", [
    ("ab", "longenough1", "Shop", "Username"),
    ("has space", "longenough1", "Shop", "Username"),
    ("ok_name", "short", "Shop", "Password"),
    ("ok_name2", "longenough1", "  ", "Business name"),
])
def test_signup_validation_messages_are_readable(username, password, name, msg):
    r = signup(username, password, name)
    assert r.status_code == 400 and r.json()["detail"].startswith(msg)


def test_wrong_password_and_unknown_user_give_the_same_answer():
    u = uname()
    signup(u)
    bad_pw, no_user = login(u, "wrong password"), login(uname())
    assert bad_pw.status_code == no_user.status_code == 401
    assert bad_pw.json() == no_user.json()   # doesn't reveal which usernames exist


def test_password_is_hashed_not_stored():
    u = uname()
    signup(u, password="my secret pass 7")
    stored = query("SELECT pw_hash FROM users WHERE username = ?", (u,))[0]["pw_hash"]
    assert "my secret pass 7" not in stored and stored.startswith("scrypt$")
    assert security.verify_password("my secret pass 7", stored)
    assert not security.verify_password("my secret pass 8", stored)
    assert not security.verify_password("anything", "garbage")


def test_accounts_see_only_their_own_campaigns():
    a, b = uname(), uname()
    ta, tb = signup(a).json()["token"], signup(b).json()["token"]
    ads.create_campaign(f"acct_{a}", "Bread", "Bread", "Families", "meta", "sales", 10, 3)
    mine = client.get("/api/me/campaigns", headers={"Authorization": f"Bearer {ta}"}).json()
    theirs = client.get("/api/me/campaigns", headers={"Authorization": f"Bearer {tb}"}).json()
    assert [c["name"] for c in mine] == ["Bread"] and theirs == []


def test_login_attempts_are_rate_limited(monkeypatch):
    monkeypatch.setattr(api, "auth_limiter", security.RateLimiter(limit=3, window_s=60))
    codes = [login(uname()).status_code for _ in range(4)]
    assert codes == [401, 401, 401, 429]
