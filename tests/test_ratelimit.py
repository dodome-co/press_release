from datetime import timedelta

import pytest

from app import ratelimit
from app.extensions import db
from app.models import Order, RateLimitEvent, User, utcnow

from .conftest import login, make_user
from .test_auth import REGISTER_DATA


@pytest.fixture(autouse=True)
def limits_on(app):
    """このファイルのテストでは、回数制限を有効にする。"""
    app.config["RATE_LIMIT_ENABLED"] = True


def _login_post(client, email="client@example.com", password="password123", ip="198.51.100.1"):
    return client.post("/login", data={"email": email, "password": password},
                       environ_overrides={"REMOTE_ADDR": ip})


def _age_all_records(minutes):
    RateLimitEvent.query.update({"created_at": utcnow() - timedelta(minutes=minutes)})
    db.session.commit()


# ================= ログイン：アカウントごとの失敗 =================
def test_five_failures_lock_the_account_even_for_the_right_password(client, client_user):
    for _ in range(5):
        assert _login_post(client, password="wrong-password").status_code == 200
    res = _login_post(client)  # 正しいパスワードでも、止める
    assert res.status_code == 429 and "試行が多すぎます" in res.get_data(as_text=True)
    assert client.get("/mypage/").status_code == 302  # ログインできていない


def test_fewer_than_five_failures_do_not_lock(client, client_user):
    for _ in range(4):
        _login_post(client, password="wrong-password")
    assert _login_post(client).status_code == 302


def test_successful_login_clears_the_failure_count(client, client_user):
    for _ in range(4):
        _login_post(client, password="wrong-password")
    assert _login_post(client).status_code == 302
    client.post("/logout")
    for _ in range(4):  # また、4回まで失敗できる
        _login_post(client, password="wrong-password")
    assert _login_post(client).status_code == 302


def test_lock_ends_after_the_period(client, client_user):
    for _ in range(5):
        _login_post(client, password="wrong-password")
    assert _login_post(client).status_code == 429
    _age_all_records(16)  # 15分より前の記録は、数えない
    assert _login_post(client).status_code == 302


def test_a_lock_on_one_account_does_not_affect_others(client, client_user):
    make_user(email="other@example.com")
    for _ in range(5):
        _login_post(client, password="wrong-password")
    assert _login_post(client, email="other@example.com").status_code == 302


def test_unregistered_email_is_limited_in_the_same_way(client):
    """登録されていないメールアドレスでも、同じように止まる（登録の有無が分からない）。"""
    for _ in range(5):
        assert _login_post(client, email="nobody@example.com", password="x" * 10).status_code == 200
    res = _login_post(client, email="nobody@example.com", password="x" * 10)
    assert res.status_code == 429 and "試行が多すぎます" in res.get_data(as_text=True)


def test_limited_response_is_the_same_for_registered_and_unregistered(client, client_user):
    for email in ("client@example.com", "nobody@example.com"):
        for _ in range(5):
            _login_post(client, email=email, password="wrong-password", ip=f"198.51.100.{len(email)}")
    a = _login_post(client, email="client@example.com", ip="198.51.100.77")
    b = _login_post(client, email="nobody@example.com", ip="198.51.100.78")
    assert a.status_code == b.status_code == 429
    assert a.get_data(as_text=True).replace("client@example.com", "") == b.get_data(as_text=True).replace("nobody@example.com", "")


def test_email_case_does_not_bypass_the_limit(client, client_user):
    for variant in ("client@example.com", "CLIENT@example.com", "Client@Example.com", " client@example.com", "client@example.com"):
        _login_post(client, email=variant, password="wrong-password")
    assert _login_post(client, email="CLIENT@EXAMPLE.COM").status_code == 429


# ================= ログイン：IP ごと =================
def test_one_ip_cannot_try_many_accounts(client, app):
    app.config["RATE_LOGIN_IP_MAX"] = 5
    for i in range(5):
        assert _login_post(client, email=f"user{i}@example.com", password="x" * 10).status_code == 200
    res = _login_post(client, email="user99@example.com", password="x" * 10)
    assert res.status_code == 429


def test_another_ip_is_not_affected(client, app, client_user):
    app.config["RATE_LOGIN_IP_MAX"] = 3
    for i in range(3):
        _login_post(client, email=f"user{i}@example.com", password="x" * 10, ip="198.51.100.1")
    assert _login_post(client, ip="198.51.100.1").status_code == 429
    assert _login_post(client, ip="198.51.100.2").status_code == 302


def test_limited_attempts_are_not_counted_again(client, client_user):
    for _ in range(5):
        _login_post(client, password="wrong-password")
    before = RateLimitEvent.query.count()
    for _ in range(3):
        assert _login_post(client).status_code == 429
    assert RateLimitEvent.query.count() == before  # 止めたぶんは、記録を増やさない（ずっと、止まり続けない）


def test_get_requests_are_not_counted(client):
    for _ in range(30):
        assert client.get("/login").status_code == 200
    assert RateLimitEvent.query.count() == 0


def test_validation_errors_are_not_counted_as_login_attempts(client):
    for _ in range(10):
        client.post("/login", data={"email": "", "password": ""})
    assert RateLimitEvent.query.count() == 0


# ================= 会員登録 =================
def test_registration_is_limited_per_ip(client, app):
    app.config["RATE_REGISTER_IP_MAX"] = 3
    for i in range(3):
        client.post("/logout")
        res = client.post("/register", data={**REGISTER_DATA, "email": f"user{i}@example.com"},
                          environ_overrides={"REMOTE_ADDR": "198.51.100.1"})
        assert res.status_code == 302
    client.post("/logout")  # ログイン中のままだと、登録の画面には、入れない
    res = client.post("/register", data={**REGISTER_DATA, "email": "user9@example.com"},
                      environ_overrides={"REMOTE_ADDR": "198.51.100.1"})
    assert res.status_code == 429 and "操作が多すぎます" in res.get_data(as_text=True)
    assert User.query.filter_by(email="user9@example.com").count() == 0
    other_ip = client.post("/register", data={**REGISTER_DATA, "email": "user9@example.com"},
                           environ_overrides={"REMOTE_ADDR": "198.51.100.2"})
    assert other_ip.status_code == 302


def test_failed_registrations_count_too(client, app):
    """入力の誤りや、登録済みのメールの確認（ありの有無の探り）も、数える。"""
    app.config["RATE_REGISTER_IP_MAX"] = 3
    for _ in range(3):
        client.post("/register", data={**REGISTER_DATA, "email": "not-an-email"})
    assert client.post("/register", data=REGISTER_DATA).status_code == 429


def test_registration_limit_ends_after_the_period(client, app):
    app.config["RATE_REGISTER_IP_MAX"] = 1
    client.post("/register", data=REGISTER_DATA)
    client.post("/logout")
    assert client.post("/register", data={**REGISTER_DATA, "email": "b@example.com"}).status_code == 429
    _age_all_records(61)
    assert client.post("/register", data={**REGISTER_DATA, "email": "b@example.com"}).status_code == 302


# ================= パスワード再設定の依頼 =================
def _reset(client, email="client@example.com", ip="198.51.100.1"):
    return client.post("/password/reset", data={"email": email}, environ_overrides={"REMOTE_ADDR": ip})


def test_reset_requests_for_one_email_stop_sending_mail_but_look_the_same(app, client, client_user):
    responses = [_reset(client, ip=f"198.51.100.{i}") for i in range(1, 6)]  # IP を変えて、5回
    assert {r.status_code for r in responses} == {200}
    assert len({r.get_data() for r in responses}) == 1  # どれも、同じ画面
    mails = [m for m in app.extensions["mail_outbox"] if m["to"] == "client@example.com"]
    assert len(mails) == 3  # 1時間に3通まで（メール爆撃を防ぐ）


def test_reset_requests_are_limited_per_ip(client, client_user, app):
    for i in range(5):
        assert _reset(client, email=f"user{i}@example.com").status_code == 200
    res = _reset(client, email="user9@example.com")
    assert res.status_code == 429 and "操作が多すぎます" in res.get_data(as_text=True)
    assert _reset(client, ip="198.51.100.2").status_code == 200


def test_reset_limit_ends_after_the_period(app, client, client_user):
    for _ in range(3):
        _reset(client)
    _reset(client)
    assert len([m for m in app.extensions["mail_outbox"] if m["to"] == "client@example.com"]) == 3
    _age_all_records(61)
    _reset(client, ip="198.51.100.9")
    assert len([m for m in app.extensions["mail_outbox"] if m["to"] == "client@example.com"]) == 4


def test_reset_for_unregistered_email_is_not_special(client):
    assert _reset(client, email="nobody@example.com").status_code == 200


# ================= 申込み =================
def test_orders_are_limited_per_user(client, client_user, app, monkeypatch):
    from app import payments

    app.config["RATE_ORDER_USER_MAX"] = 3
    app.config["MAX_UNPAID_ORDERS"] = 100
    monkeypatch.setattr(payments, "create_checkout_session",
                        lambda order, *a, **k: {"id": f"cs_{order.id}", "url": "https://checkout.stripe.com/c/pay/x"})
    monkeypatch.setattr(payments, "expire_checkout_session", lambda sid: None)
    login(client, "client@example.com")
    for _ in range(3):
        assert client.post("/services/distribution_proxy/order").status_code == 303
    res = client.post("/services/distribution_proxy/order", follow_redirects=True)
    assert "申込みの操作が多すぎます" in res.get_data(as_text=True)
    assert Order.query.count() == 3


def test_order_limit_is_per_user(client, client_user, app, monkeypatch):
    from app import payments

    app.config["RATE_ORDER_USER_MAX"] = 1
    monkeypatch.setattr(payments, "create_checkout_session",
                        lambda order, *a, **k: {"id": f"cs_{order.id}", "url": "https://checkout.stripe.com/c/pay/x"})
    login(client, "client@example.com")
    assert client.post("/services/distribution_proxy/order").status_code == 303
    other = app.test_client()
    make_user(email="other@example.com")
    login(other, "other@example.com")
    assert other.post("/services/distribution_proxy/order").status_code == 303


# ================= 仕組み =================
def test_nothing_is_limited_when_disabled(client, client_user, app):
    app.config["RATE_LIMIT_ENABLED"] = False
    for _ in range(30):
        _login_post(client, password="wrong-password")
    assert _login_post(client).status_code == 302
    assert RateLimitEvent.query.count() == 0


def test_records_do_not_contain_the_email_or_the_ip(client, client_user):
    _login_post(client, email="client@example.com", password="wrong-password", ip="203.0.113.77")
    rows = RateLimitEvent.query.all()
    assert rows and all(len(r.key_hash) == 64 for r in rows)
    flat = " ".join(f"{r.scope} {r.key_hash}" for r in rows)
    assert "client@example.com" not in flat and "203.0.113.77" not in flat


def test_old_records_are_cleaned_up(app):
    db.session.add(RateLimitEvent(scope="login_ip", key_hash="a" * 64, created_at=utcnow() - timedelta(days=3)))
    db.session.add(RateLimitEvent(scope="login_ip", key_hash="b" * 64, created_at=utcnow()))
    db.session.commit()
    app.extensions.pop("ratelimit_cleaned_at", None)
    ratelimit.record(ratelimit.login_attempts_per_ip(), "1.2.3.4")
    keys = {r.key_hash for r in RateLimitEvent.query.all()}
    assert "a" * 64 not in keys and "b" * 64 in keys


def test_cleanup_runs_at_most_once_an_hour(app):
    db.session.add(RateLimitEvent(scope="x", key_hash="a" * 64, created_at=utcnow() - timedelta(days=3)))
    db.session.commit()
    ratelimit.record(ratelimit.login_attempts_per_ip(), "1.2.3.4")  # 1回目：消える
    db.session.add(RateLimitEvent(scope="x", key_hash="c" * 64, created_at=utcnow() - timedelta(days=3)))
    db.session.commit()
    ratelimit.record(ratelimit.login_attempts_per_ip(), "1.2.3.4")  # 1時間以内：消さない
    assert "c" * 64 in {r.key_hash for r in RateLimitEvent.query.all()}


def test_scopes_do_not_mix(app):
    a, b = ratelimit.login_attempts_per_ip(), ratelimit.register_per_ip()
    for _ in range(a.max_count):
        ratelimit.record(a, "1.2.3.4")
    assert ratelimit.is_limited(a, "1.2.3.4") and not ratelimit.is_limited(b, "1.2.3.4")


def test_defaults(app):
    assert (ratelimit.login_failures_per_account().max_count, ratelimit.login_failures_per_account().period_seconds) == (5, 900)
    assert (ratelimit.login_attempts_per_ip().max_count, ratelimit.login_attempts_per_ip().period_seconds) == (20, 600)
    assert (ratelimit.register_per_ip().max_count, ratelimit.register_per_ip().period_seconds) == (10, 3600)
    assert (ratelimit.reset_request_per_ip().max_count, ratelimit.reset_request_per_email().max_count) == (5, 3)
    assert ratelimit.order_per_user().max_count == 10


def test_enabled_by_default_outside_tests():
    from app.config import Config, DemoConfig, DevelopmentConfig, ProductionConfig, TestingConfig

    assert all(c.RATE_LIMIT_ENABLED for c in (Config, DevelopmentConfig, ProductionConfig, DemoConfig))
    assert TestingConfig.RATE_LIMIT_ENABLED is False


def test_check_and_record_stops_exactly_at_the_limit(app):
    limit = ratelimit.Limit("t", 3, 60)
    assert [ratelimit.check_and_record(limit, "k") for _ in range(5)] == [True, True, True, False, False]
    assert RateLimitEvent.query.count() == 3
