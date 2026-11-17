"""段階3のセキュリティ点検（二重払い・識別キー・URL・設定など）への対策のテスト。"""
import logging

import pytest
from sqlalchemy.exc import IntegrityError

from app import create_app, payments
from app.config import DevelopmentConfig, ProductionConfig
from app.extensions import db
from app.models import Order, OrderCheckoutSession, User
from app.orders import service as order_service
from app.textutil import is_safe_http_url

from .conftest import login
from .test_orders import (  # noqa: F401  フィクスチャとして使う
    _event,
    _exhaust_free_slot,
    _mails,
    _order,
    gateway,
    logged_in,
    webhook,
)


# ---- 1. 古い支払い画面は、新しく作る前・取り消す前に無効にする ----
def test_starting_checkout_again_expires_previous_sessions(client, logged_in, gateway):
    order = _order(logged_in)
    first = f"cs_order_{order.id}"
    client.post(f"/orders/{order.id}/pay")
    assert gateway["expired"] == [first]  # 新しい画面を作る前に、元の画面を無効にする
    second = f"cs_test_{order.id}_1"
    client.post(f"/orders/{order.id}/pay")
    assert set(gateway["expired"]) == {first, second}  # 2回目は、元の画面と、さっき作った画面


def test_cancel_expires_all_sessions(client, logged_in, gateway):
    order = _order(logged_in)
    client.post(f"/orders/{order.id}/pay")
    gateway["expired"].clear()
    client.post(f"/orders/{order.id}/cancel")
    assert sorted(gateway["expired"]) == sorted(r.stripe_session_id for r in order.checkout_sessions)
    assert len(gateway["expired"]) == 2


def test_every_session_is_recorded_in_history(client, logged_in, gateway):
    order = _order(logged_in)
    client.post(f"/orders/{order.id}/pay")
    client.post(f"/orders/{order.id}/pay")
    db.session.refresh(order)
    assert len(order.checkout_sessions) == 3
    assert order.stripe_session_id == f"cs_test_{order.id}_2"


# ---- 2. 識別キー（idempotency key）が、毎回変わる ----
def test_attempt_number_increases_every_time(client, logged_in, gateway):
    _exhaust_free_slot(logged_in)
    for _ in range(3):  # 「購入 → 戻る → もう一度購入」を繰り返す
        client.post("/services/release_creation/order")
    assert [c["attempt"] for c in gateway["created"]] == [1, 2, 3]
    assert Order.query.one().checkout_attempts == 3


def test_attempt_is_not_based_on_time(client, logged_in, gateway):
    order = _order(logged_in)
    client.post(f"/orders/{order.id}/pay")
    client.post(f"/orders/{order.id}/pay")  # 同じ秒内でも、別の値になる
    assert [c["attempt"] for c in gateway["created"]] == [1, 2]


# ---- 3. 二重払いの検出 ----
def test_second_payment_from_old_session_is_reported(app, client, logged_in, gateway, webhook, caplog):
    admin = User(email="boss@example.com", password_hash="x", role="admin")
    db.session.add(admin)
    order = _order(logged_in)
    db.session.commit()
    client.post(f"/orders/{order.id}/pay")  # 2つ目の支払い画面
    db.session.refresh(order)
    old, new = f"cs_order_{order.id}", order.stripe_session_id
    webhook(_event("evt_new", order, id=new, payment_intent="pi_first"))
    db.session.refresh(order)
    assert order.status == "paid" and order.stripe_payment_intent_id == "pi_first"

    with caplog.at_level(logging.ERROR):
        res = webhook(_event("evt_old", order, id=old, payment_intent="pi_second"))
    assert res.status_code == 200
    assert "二重に支払われた可能性" in caplog.text
    mail = [m for m in _mails(app) if m["to"] == "boss@example.com"]
    assert len(mail) == 1 and "pi_second" in mail[0]["body"] and f"注文番号：{order.id}" in mail[0]["body"]
    db.session.refresh(order)
    assert order.stripe_payment_intent_id == "pi_first"  # 最初の支払いのまま


def test_same_payment_resent_is_not_reported_as_duplicate(app, logged_in, webhook):
    db.session.add(User(email="boss@example.com", password_hash="x", role="admin"))
    order = _order(logged_in)
    db.session.commit()
    webhook(_event("evt_a", order, payment_intent="pi_same"))
    webhook(_event("evt_b", order, payment_intent="pi_same"))  # 別のイベントでも、同じ支払い
    assert not [m for m in _mails(app) if m["to"] == "boss@example.com"]


# ---- 5. 通知のセッションが、この注文のものか ----
def test_payment_from_unknown_session_is_not_confirmed(logged_in, webhook, caplog):
    order = _order(logged_in)
    with caplog.at_level(logging.ERROR):
        assert webhook(_event("evt_1", order, id="cs_from_somewhere_else")).status_code == 200
    db.session.refresh(order)
    assert order.status == "unpaid" and "この注文のものではない" in caplog.text


def test_session_of_another_order_is_not_accepted(logged_in, webhook):
    a, b = _order(logged_in), _order(logged_in)
    webhook(_event("evt_1", a, id=b.stripe_session_id))  # b の画面で払われたのに、a の注文を指している
    db.session.refresh(a)
    assert a.status == "unpaid"


# ---- 8. 支払い画面を作っている間に、状態が変わった ----
def test_order_paid_while_creating_session_discards_new_session(client, logged_in, gateway, monkeypatch):
    order = _order(logged_in)
    original = payments.create_checkout_session

    def create_then_pay_elsewhere(order_, *a, **k):
        session = original(order_, *a, **k)
        Order.query.filter_by(id=order_.id).update({"status": "paid"})  # 別のタブで支払いが済んだ
        db.session.commit()
        return session

    monkeypatch.setattr(payments, "create_checkout_session", create_then_pay_elsewhere)
    res = client.post(f"/orders/{order.id}/pay", follow_redirects=True)
    assert "すでにお支払い済み" in res.get_data(as_text=True)
    db.session.refresh(order)
    assert order.status == "paid"
    assert order.stripe_session_id == f"cs_order_{order.id}"  # 上書きされない
    assert f"cs_test_{order.id}_1" in gateway["expired"]  # 新しく作った画面は無効にする


# ---- 7. Stripe の応答の URL ----
@pytest.mark.parametrize("url", [None, "", "http://checkout.stripe.com/x", "https://evil.example.com/x",
                                 "https://checkout.stripe.com.evil.example/x", "javascript:alert(1)"])
def test_unexpected_checkout_url_is_refused(client, logged_in, monkeypatch, url):
    monkeypatch.setattr(payments, "create_checkout_session", lambda *a, **k: {"id": "cs_x", "url": url})
    monkeypatch.setattr(payments, "expire_checkout_session", lambda sid: None)
    res = client.post("/services/distribution_proxy/order", follow_redirects=False)
    assert res.status_code == 302 and "/services" in res.headers["Location"]
    assert OrderCheckoutSession.query.count() == 0


def test_stripe_domains_are_accepted(client, logged_in, monkeypatch):
    monkeypatch.setattr(payments, "create_checkout_session",
                        lambda *a, **k: {"id": "cs_x", "url": "https://checkout.stripe.com/c/pay/cs_x#abc"})
    assert client.post("/services/distribution_proxy/order").status_code == 303


# ---- 4. 素材リンクの検証 ----
@pytest.mark.parametrize("url", [
    "https://a.example/x\nINJECT", "https://a.example/x\tfoo", "https://a.example/x\rfoo", "https://a.example/ x",
    "https://　x.example", "http://", "https:///path", "https://", "javascript:alert(1)",
    "HTTPS://user:pw@host.example/", "https://user@host.example/", "https://host.example:99999/",
    "https://host.example:abc/", "ftp://host.example/", " https://host.example/", "https://host.example/ ",
    "https://" + "a" * 600 + ".example", "//host.example/x", "https://host.example/\x00",
])
def test_bad_materials_urls_are_rejected(url):
    assert not is_safe_http_url(url)


@pytest.mark.parametrize("url", [
    "https://drive.example.com/folder/abc?x=1&y=2#top", "http://example.com", "https://example.com:8443/path",
    "HTTPS://Example.COM/Path", "https://日本語.example/パス",
])
def test_good_materials_urls_are_accepted(url):
    assert is_safe_http_url(url)


def test_order_with_multiline_materials_url_is_refused(client, logged_in, gateway):
    res = client.post("/services/video_creation/order", data={"materials_url": "https://a.example/x\nINJECT"},
                      follow_redirects=True)
    assert "素材の共有リンク" in res.get_data(as_text=True)
    assert Order.query.count() == 0


def test_profile_website_url_is_strict_too(client, client_user):
    login(client, "client@example.com")
    data = {"company_name": "社", "postal_code": "100-0001", "address": "住所", "contact_name": "担当",
            "phone": "03-0000-0000"}
    for bad in ("https://a.example/x\nb", "https://user:pw@a.example", "https://"):
        res = client.post("/mypage/profile", data={**data, "website_url": bad})
        assert res.status_code == 200
    assert client.post("/mypage/profile", data={**data, "website_url": "https://a.example"}).status_code == 302


# ---- 申込みの連打 ----
def test_unpaid_orders_are_limited(client, logged_in, gateway, app):
    app.config["MAX_UNPAID_ORDERS"] = 3
    for _ in range(3):
        assert client.post("/services/distribution_proxy/order").status_code == 303
    res = client.post("/services/distribution_proxy/order", follow_redirects=True)
    assert "お支払い前の注文が多すぎます" in res.get_data(as_text=True)
    assert Order.query.count() == 3 and len(gateway["created"]) == 3
    client.post("/orders/1/cancel")  # 取り消せば、また申し込める
    assert client.post("/services/distribution_proxy/order").status_code == 303


def test_paid_orders_do_not_count_toward_unpaid_limit(client, logged_in, gateway, app):
    app.config["MAX_UNPAID_ORDERS"] = 1
    _order(logged_in, status="paid")
    assert client.post("/services/distribution_proxy/order").status_code == 303


# ---- 6. 設定 ----
def test_development_refuses_live_stripe_key(monkeypatch):
    monkeypatch.setattr(DevelopmentConfig, "STRIPE_SECRET_KEY", "sk_live_abc")
    with pytest.raises(RuntimeError, match="sk_live_"):
        create_app("development")
    monkeypatch.setattr(DevelopmentConfig, "STRIPE_SECRET_KEY", "sk_test_abc")
    create_app("development")


def _production(monkeypatch, key, allow_test=False):
    for name, value in (("SECRET_KEY", "x"), ("BASE_URL", "https://e.com"), ("TRUSTED_HOSTS", ["e.com"]),
                        ("MAIL_BACKEND", "smtp"), ("STRIPE_SECRET_KEY", key), ("STRIPE_WEBHOOK_SECRET", "whsec_x")):
        monkeypatch.setattr(ProductionConfig, name, value)
    if allow_test:
        monkeypatch.setenv("ALLOW_TEST_STRIPE_KEY", "1")
    else:
        monkeypatch.delenv("ALLOW_TEST_STRIPE_KEY", raising=False)


def test_production_refuses_test_key_unless_explicitly_allowed(monkeypatch):
    _production(monkeypatch, "sk_test_abc")
    with pytest.raises(RuntimeError, match="本番用のキー"):
        create_app("production")
    _production(monkeypatch, "sk_test_abc", allow_test=True)
    create_app("production")


def test_production_accepts_live_key(monkeypatch):
    _production(monkeypatch, "sk_live_abc")
    create_app("production")


def test_testing_config_ignores_keys_from_dotenv(app):
    assert app.config["STRIPE_SECRET_KEY"] == "" and app.config["STRIPE_WEBHOOK_SECRET"] == ""


@pytest.mark.parametrize("value", ["5,000", "abc", "0", "49", "-100", "100000000", "1.5"])
def test_bad_price_override_stops_startup(monkeypatch, value):
    monkeypatch.setenv("PRICE_VIDEO_CREATION", value)
    with pytest.raises(RuntimeError, match="PRICE_VIDEO_CREATION"):
        create_app("testing")


def test_good_price_override_is_accepted(monkeypatch):
    monkeypatch.setenv("PRICE_VIDEO_CREATION", "60000")
    create_app("testing")


def test_amount_must_be_at_least_50_yen(app, client_user):
    for amount in (0, 49):
        db.session.add(Order(user_id=client_user.id, service_type="x", service_name="x", amount_yen=amount))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()
    db.session.add(Order(user_id=client_user.id, service_type="x", service_name="x", amount_yen=50))
    db.session.commit()


def test_webhook_misconfiguration_is_logged(client, app, caplog):
    app.config["STRIPE_WEBHOOK_SECRET"] = ""
    with caplog.at_level(logging.ERROR):
        assert client.post("/stripe/webhook", data=b"{}").status_code == 400
    assert "STRIPE_WEBHOOK_SECRET" in caplog.text


def test_huge_order_id_in_event_is_ignored_not_500(logged_in, webhook):
    order = _order(logged_in)
    event = _event("evt_1", order, metadata={"order_id": "9" * 23}, client_reference_id="9" * 23)
    assert webhook(event).status_code == 200
    db.session.refresh(order)
    assert order.status == "unpaid"


def test_session_history_is_deleted_with_order(app, client_user):
    order = _order(client_user)
    assert OrderCheckoutSession.query.count() == 1
    db.session.delete(order)
    db.session.commit()
    assert OrderCheckoutSession.query.count() == 0


def test_start_checkout_refuses_paid_order(app, client_user, gateway):
    order = _order(client_user, status="paid")
    with pytest.raises(order_service.OrderError):
        order_service.start_checkout(order)
    assert gateway["created"] == []


# ---- 本物の Stripe のオブジェクトを通す（模擬が、本物の形と違っていた不具合の再発防止） ----
def _real_session(order_id=1):
    import stripe

    return stripe.checkout.Session.construct_from(
        {"id": "cs_test_real", "object": "checkout.session", "url": "https://checkout.stripe.com/c/pay/cs_test_real",
         "payment_status": "unpaid", "metadata": {"order_id": str(order_id)}}, "sk_test_x")


def test_real_stripe_session_object_works_end_to_end(app, client, logged_in, monkeypatch):
    """gateway を差し替えず、Stripe のライブラリ（のネットワーク呼び出しだけ）を差し替える。"""
    import stripe

    app.config["STRIPE_SECRET_KEY"] = "sk_test_x"
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return _real_session()

    monkeypatch.setattr(stripe.checkout.Session, "create", fake_create)
    monkeypatch.setattr(payments, "expire_checkout_session", lambda sid: None)
    res = client.post("/services/distribution_proxy/order")
    assert res.status_code == 303 and res.headers["Location"] == "https://checkout.stripe.com/c/pay/cs_test_real"
    assert Order.query.one().stripe_session_id == "cs_test_real"
    assert captured["line_items"][0]["price_data"]["unit_amount"] == 3000
    assert captured["line_items"][0]["price_data"]["currency"] == "jpy"
    assert captured["idempotency_key"] == "checkout-order-1-1"
    assert captured["metadata"] == {"order_id": "1"}


def test_gateway_returns_plain_dict(app, monkeypatch):
    import stripe

    app.config["STRIPE_SECRET_KEY"] = "sk_test_x"
    monkeypatch.setattr(stripe.checkout.Session, "create", lambda **kw: _real_session())
    order = Order(id=1, user_id=None, service_type="x", service_name="x", amount_yen=100)
    result = payments.create_checkout_session(order, "a@example.com", "https://e/ok", "https://e/ng", 1)
    assert type(result) is dict and set(result) == {"id", "url"}


def test_stripe_api_error_becomes_message(app, client, logged_in, monkeypatch):
    import stripe

    app.config["STRIPE_SECRET_KEY"] = "sk_test_x"  # キーがあって、Stripe 側がエラーを返す場合
    monkeypatch.setattr(stripe.checkout.Session, "create",
                        lambda **kw: (_ for _ in ()).throw(stripe.AuthenticationError("bad key")))
    res = client.post("/services/distribution_proxy/order", follow_redirects=True)
    assert res.status_code == 200 and "決済を開始できませんでした" in res.get_data(as_text=True)
    assert Order.query.one().status == "unpaid"
