from unittest.mock import patch

import pytest

from app import payments
from app.extensions import db
from app.models import Order, OrderCheckoutSession, Release, StripeEvent, utcnow
from app.releases import service as release_service

from .conftest import login, make_user
from .test_releases import FULL


@pytest.fixture()
def gateway(monkeypatch):
    """Stripe の代わりに、呼ばれた内容を記録するだけのもの。"""
    calls = {"created": [], "expired": []}

    def create(order, customer_email, success_url, cancel_url, attempt=1):
        calls["created"].append(dict(order_id=order.id, amount=order.amount_yen, name=order.service_name,
                                     email=customer_email, success=success_url, cancel=cancel_url, attempt=attempt))
        return {"id": f"cs_test_{order.id}_{attempt}", "url": f"https://checkout.stripe.com/c/pay/{order.id}-{attempt}"}

    monkeypatch.setattr(payments, "create_checkout_session", create)
    monkeypatch.setattr(payments, "expire_checkout_session", lambda sid: calls["expired"].append(sid))
    return calls


@pytest.fixture()
def logged_in(client, client_user):
    login(client, "client@example.com")
    return client_user


def _exhaust_free_slot(user):
    db.session.add(Release(user_id=user.id, first_submitted_at=utcnow(), **FULL))
    db.session.commit()


def _order(user, service_type="interview_planning", amount=60000, status="unpaid", **kw):
    """注文を作る。支払い画面（セッション）も、1つ作ったことにする。"""
    o = Order(user_id=user.id, service_type=service_type, service_name="テスト", amount_yen=amount, status=status, **kw)
    db.session.add(o)
    db.session.commit()
    session_id = o.stripe_session_id or f"cs_order_{o.id}"
    o.stripe_session_id = session_id
    db.session.add(OrderCheckoutSession(order_id=o.id, stripe_session_id=session_id))
    db.session.commit()
    return o


def _event(event_id, order, event_type="checkout.session.completed", **overrides):
    obj = {"id": order.stripe_session_id, "payment_status": "paid", "amount_total": order.amount_yen,
           "currency": "jpy", "payment_intent": f"pi_{event_id}", "client_reference_id": str(order.id),
           "metadata": {"order_id": str(order.id)}}
    obj.update(overrides)
    return {"id": event_id, "type": event_type, "data": {"object": obj}}


@pytest.fixture()
def webhook(client, monkeypatch):
    """署名の確認を通ったことにして、イベントを送る。"""
    def post(event):
        monkeypatch.setattr(payments, "construct_event", lambda payload, sig: event)
        return client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "t=1,v1=x"})
    return post


def _mails(app):
    return app.extensions.get("mail_outbox", [])


# ---- サービス案内 ----
def test_services_page_is_public_but_ordering_needs_a_client_login(client, admin_user, app):
    # ログインしていなくても、サービスの一覧は見られる（申し込みは、ログインしたクライアントだけ）
    res = client.get("/services")
    assert res.status_code == 200 and "サービスのご案内" in res.get_data(as_text=True)
    assert client.post("/services/distribution_proxy/order").status_code == 302  # 申込みは、ログインへ
    login(client, "admin@example.com")
    assert client.get("/services").status_code == 200  # 管理者は、見るだけ
    assert client.post("/services/distribution_proxy/order").status_code == 403


def test_services_page_lists_all_with_prices_and_policy(client, logged_in):
    body = client.get("/services").get_data(as_text=True)
    for name in ("記者発表会の企画・当日の同席", "投げ込み交渉代行", "自社媒体などに掲載する記事制作",
                 "自社媒体などに掲載する動画制作"):
        assert name in body
    for price in ("3,000円", "10,000円", "30,000円"):
        assert price in body
    assert "取材会の同行" not in body  # 記者発表会に統合した
    assert "キャンセル・返金について" in body and "全額" in body and "50%" in body
    assert "当日、取材するメディアが一件もなければ、全額返金します" in body


def test_services_page_notes_come_before_the_price_note(client, logged_in):
    body = client.get("/services").get_data(as_text=True)
    notes = ("※リリース作成以外のサービスは、面談が必須となります。", "※複数のサービスを申し込む場合は、ご相談ください。",
             "※その他、ご希望のサービスがあれば、ご相談ください。")
    price = "料金は税込です。"
    positions = [body.index(n) for n in notes]
    assert positions == sorted(positions) and positions[-1] < body.index(price)  # 3つの注意書きが、順に並び、税込の説明より前


def test_services_page_shows_regional_prices_as_a_choice(client, logged_in):
    body = client.get("/services").get_data(as_text=True)
    for label, price in (("岩手県内", "80,000円"), ("宮城県・青森県・秋田県", "90,000円"), ("福島県・山形県", "100,000円"),
                         ("その他", "120,000円")):
        assert label in body and price in body
    assert body.count('name="option"') == 4  # 地域の選択肢は、記者発表会だけ


def test_refund_policy_is_shown_as_a_list(client, logged_in):
    body = client.get("/services").get_data(as_text=True)
    section = body[body.index("キャンセル・返金について"):]
    assert section.count("<li>") >= 7  # 見出し4つ＋その下の3つ
    assert "なお、" not in section  # 分かりにくかった「なお」以下の1文は、項目に分けた
    assert "お問い合わせ先：（準備中）" in body


def test_release_creation_not_purchasable_while_free_slot_remains(client, logged_in):
    body = client.get("/services").get_data(as_text=True)
    assert "無料の枠が残っているので" in body
    assert "/services/release_creation/order" not in body


def test_release_creation_purchasable_after_free_slot_used(client, logged_in):
    _exhaust_free_slot(logged_in)
    body = client.get("/services").get_data(as_text=True)
    assert "/services/release_creation/order" in body and "4,000円" in body


def test_releases_index_links_to_purchase_when_exhausted(client, logged_in):
    _exhaust_free_slot(logged_in)
    body = client.get("/releases/").get_data(as_text=True)
    assert "枠を使い切りました" in body and "/services" in body


# ---- 申込み ----
def test_order_uses_server_side_price_and_redirects_to_stripe(client, logged_in, gateway):
    res = client.post("/services/distribution_proxy/order", data={"amount_yen": "1", "amount": "1"})
    assert res.status_code == 303 and res.headers["Location"] == "https://checkout.stripe.com/c/pay/1-1"
    order = Order.query.one()
    assert (order.amount_yen, order.status, order.user_id) == (3000, "unpaid", logged_in.id)
    assert order.stripe_session_id == "cs_test_1_1"
    call = gateway["created"][0]
    assert call["amount"] == 3000 and call["email"] == "client@example.com"  # 画面から送った金額は使われない
    assert call["success"].endswith("?session_id={CHECKOUT_SESSION_ID}")
    assert call["cancel"].endswith("/orders/1/cancel")


def test_unknown_service_is_404(client, logged_in, gateway):
    assert client.post("/services/nope/order").status_code == 404
    assert Order.query.count() == 0


def test_video_requires_valid_materials_url(client, logged_in, gateway):
    for bad in ("", "  ", "javascript:alert(1)", "ftp://example.com/x", "not a url", "https://a b"):
        res = client.post("/services/video_creation/order", data={"materials_url": bad}, follow_redirects=True)
        assert "素材" in res.get_data(as_text=True)
    assert Order.query.count() == 0 and gateway["created"] == []
    res = client.post("/services/video_creation/order", data={"materials_url": "https://drive.example.com/folder/abc"})
    assert res.status_code == 303
    assert Order.query.one().materials_url == "https://drive.example.com/folder/abc"


def test_materials_url_ignored_for_other_services(client, logged_in, gateway):
    client.post("/services/article_creation/order", data={"materials_url": "https://x.example.com"})
    assert Order.query.one().materials_url is None


def test_release_creation_refused_while_free_slot_remains(client, logged_in, gateway):
    res = client.post("/services/release_creation/order", follow_redirects=True)
    assert "無料の枠がまだ残っている" in res.get_data(as_text=True)
    assert Order.query.count() == 0


def test_release_creation_reuses_existing_unpaid_order(client, logged_in, gateway):
    _exhaust_free_slot(logged_in)
    client.post("/services/release_creation/order")
    client.post("/services/release_creation/order")
    assert Order.query.count() == 1
    assert len(gateway["created"]) == 2  # 支払い画面は、新しく作り直す


def test_stripe_failure_is_a_message_not_a_500(client, logged_in, monkeypatch):
    def boom(*a, **k):
        raise payments.PaymentError("x")

    monkeypatch.setattr(payments, "create_checkout_session", boom)
    res = client.post("/services/distribution_proxy/order", follow_redirects=True)
    assert res.status_code == 200 and "決済を開始できませんでした" in res.get_data(as_text=True)
    assert Order.query.one().status == "unpaid"


# ---- 完了ページ・中断ページ ----
def test_success_page_does_not_confirm_payment(client, logged_in, gateway):
    order = _order(logged_in)
    body = client.get(f"/orders/{order.id}/success?session_id=cs_whatever&payment=ok").get_data(as_text=True)
    assert "お支払いを確認しています" in body
    db.session.refresh(order)
    assert order.status == "unpaid" and order.paid_at is None


def test_success_page_shows_paid_after_webhook(client, logged_in):
    order = _order(logged_in, status="paid", paid_at=utcnow(), progress="received")
    assert "お支払いを確認しました" in client.get(f"/orders/{order.id}/success").get_data(as_text=True)


def test_other_users_orders_are_404(app, client, logged_in):
    other = make_user(email="other@example.com")
    order = _order(other)
    for path in ("success", "cancel"):
        assert client.get(f"/orders/{order.id}/{path}").status_code == 404
    assert client.post(f"/orders/{order.id}/cancel").status_code == 404
    assert client.post(f"/orders/{order.id}/pay").status_code == 404


def test_cancel_unpaid_order_expires_stripe_session(client, logged_in, gateway):
    order = _order(logged_in, stripe_session_id="cs_abc")
    res = client.post(f"/orders/{order.id}/cancel")
    assert res.status_code == 302
    db.session.refresh(order)
    assert order.status == "canceled" and gateway["expired"] == ["cs_abc"]


def test_paid_order_cannot_be_canceled(client, logged_in, gateway):
    order = _order(logged_in, status="paid")
    res = client.post(f"/orders/{order.id}/cancel", follow_redirects=True)
    assert "取り消せません" in res.get_data(as_text=True)
    db.session.refresh(order)
    assert order.status == "paid" and gateway["expired"] == []


def test_pay_again_creates_new_session(client, logged_in, gateway):
    order = _order(logged_in, stripe_session_id="cs_old")
    res = client.post(f"/orders/{order.id}/pay")
    assert res.status_code == 303
    db.session.refresh(order)
    assert order.stripe_session_id != "cs_old"


def test_cannot_pay_a_paid_or_canceled_order(client, logged_in, gateway):
    for status in ("paid", "canceled"):
        order = _order(logged_in, status=status)
        res = client.post(f"/orders/{order.id}/pay", follow_redirects=True)
        assert "お支払い済みか、取り消されています" in res.get_data(as_text=True)
    assert gateway["created"] == []


# ---- Webhook ----
def test_webhook_rejects_bad_signature(client, monkeypatch):
    def bad(payload, sig):
        raise payments.SignatureError("bad", sig)

    monkeypatch.setattr(payments, "construct_event", bad)
    assert client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "x"}).status_code == 400
    assert StripeEvent.query.count() == 0


def test_webhook_rejects_when_secret_not_configured(client, app):
    app.config["STRIPE_WEBHOOK_SECRET"] = ""
    assert client.post("/stripe/webhook", data=b"{}").status_code == 400


def test_webhook_real_signature_check(client, app, logged_in):
    """署名の確認そのものは、Stripe の部品が本当に行う（模擬しない）。"""
    import hashlib
    import hmac
    import json
    import time

    app.config["STRIPE_WEBHOOK_SECRET"] = "whsec_test"
    order = _order(logged_in)
    payload = json.dumps(_event("evt_real", order)).encode()
    ts = int(time.time())
    sig = hmac.new(b"whsec_test", f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    ok = client.post("/stripe/webhook", data=payload, headers={"Stripe-Signature": f"t={ts},v1={sig}"})
    assert ok.status_code == 200
    db.session.refresh(order)
    assert order.status == "paid"
    forged = client.post("/stripe/webhook", data=payload, headers={"Stripe-Signature": f"t={ts},v1={'0' * 64}"})
    assert forged.status_code == 400


def test_webhook_needs_no_csrf_token(app, logged_in, webhook):
    app.config["WTF_CSRF_ENABLED"] = True
    order = _order(logged_in)
    assert webhook(_event("evt_1", order)).status_code == 200


def test_paid_event_marks_order_paid_and_sends_confirmation(app, logged_in, webhook):
    order = _order(logged_in)
    assert webhook(_event("evt_1", order)).status_code == 200
    db.session.refresh(order)
    assert order.status == "paid" and order.progress == "received" and order.paid_at is not None
    assert order.stripe_payment_intent_id == "pi_evt_1"
    assert StripeEvent.query.one().event_id == "evt_1"
    mail = _mails(app)[-1]
    assert mail["to"] == "client@example.com" and "60,000円" in mail["body"] and "返金" in mail["body"]
    assert "お問い合わせ先：（準備中）" in mail["body"]


def test_release_creation_paid_has_no_progress_and_adds_slot(app, logged_in, webhook):
    _exhaust_free_slot(logged_in)
    assert not release_service.can_create(logged_in)
    order = _order(logged_in, service_type="release_creation", amount=5000)
    webhook(_event("evt_1", order))
    db.session.refresh(order)
    assert order.status == "paid" and order.progress is None
    assert release_service.can_create(logged_in)


def test_duplicate_event_is_processed_once(app, logged_in, webhook):
    order = _order(logged_in)
    event = _event("evt_dup", order)
    assert webhook(event).status_code == 200
    assert webhook(event).status_code == 200
    assert StripeEvent.query.count() == 1
    assert len([m for m in _mails(app) if m["to"] == "client@example.com"]) == 1


def test_second_event_for_paid_order_does_nothing(app, logged_in, webhook):
    order = _order(logged_in)
    webhook(_event("evt_a", order))
    webhook(_event("evt_b", order, payment_intent="pi_other"))
    db.session.refresh(order)
    assert order.stripe_payment_intent_id == "pi_evt_a"
    assert len(_mails(app)) == 1


def test_amount_mismatch_is_not_confirmed(app, logged_in, webhook):
    order = _order(logged_in)
    assert webhook(_event("evt_1", order, amount_total=1)).status_code == 200  # 再送を止める（人が確認する）
    db.session.refresh(order)
    assert order.status == "unpaid" and _mails(app) == []


def test_wrong_currency_is_not_confirmed(logged_in, webhook):
    order = _order(logged_in)
    webhook(_event("evt_1", order, currency="usd"))
    db.session.refresh(order)
    assert order.status == "unpaid"


def test_unpaid_payment_status_is_not_confirmed(logged_in, webhook):
    order = _order(logged_in)
    webhook(_event("evt_1", order, payment_status="unpaid"))
    db.session.refresh(order)
    assert order.status == "unpaid"
    webhook(_event("evt_2", order, event_type="checkout.session.async_payment_succeeded"))
    db.session.refresh(order)
    assert order.status == "paid"  # 後払いが確定したら、そのイベントで確定する


def test_event_for_unknown_order_is_acknowledged(app, logged_in, webhook):
    order = _order(logged_in)
    event = _event("evt_1", order, metadata={"order_id": "9999"}, client_reference_id="9999")
    assert webhook(event).status_code == 200
    db.session.refresh(order)
    assert order.status == "unpaid"


def test_expired_event_cancels_matching_unpaid_order(logged_in, webhook):
    order = _order(logged_in, stripe_session_id="cs_1")
    webhook(_event("evt_1", order, event_type="checkout.session.expired", id="cs_1", payment_status="unpaid"))
    db.session.refresh(order)
    assert order.status == "canceled"


def test_expired_old_session_does_not_cancel_retried_order(logged_in, webhook):
    order = _order(logged_in, stripe_session_id="cs_new")
    webhook(_event("evt_1", order, event_type="checkout.session.expired", id="cs_old", payment_status="unpaid"))
    db.session.refresh(order)
    assert order.status == "unpaid"


def test_payment_after_cancellation_is_still_recorded(logged_in, webhook):
    order = _order(logged_in, status="canceled")
    webhook(_event("evt_1", order))
    db.session.refresh(order)
    assert order.status == "paid"  # お金は受け取っているので、記録する


def test_processing_error_returns_500_and_allows_retry(app, logged_in, webhook):
    order = _order(logged_in)
    event = _event("evt_1", order)
    with patch("app.orders.service._on_paid", side_effect=RuntimeError("boom")):
        assert webhook(event).status_code == 500
    assert StripeEvent.query.count() == 0  # 記録も巻き戻る
    assert webhook(event).status_code == 200  # 再送されたら、今度は処理される
    db.session.refresh(order)
    assert order.status == "paid"


def test_mail_failure_does_not_undo_payment(app, logged_in, webhook):
    app.config["MAIL_BACKEND"] = "not-implemented"
    order = _order(logged_in)
    assert webhook(_event("evt_1", order)).status_code == 200
    db.session.refresh(order)
    assert order.status == "paid"


def test_unhandled_event_type_is_recorded_and_ignored(logged_in, webhook):
    order = _order(logged_in)
    assert webhook(_event("evt_1", order, event_type="charge.succeeded")).status_code == 200
    db.session.refresh(order)
    assert order.status == "unpaid" and StripeEvent.query.count() == 1


# ---- 購入した枠で、2件目を提出できる（全体の流れ） ----
def test_buying_a_slot_unlocks_second_release(app, client, logged_in, gateway, webhook):
    _exhaust_free_slot(logged_in)
    res = client.post("/releases/new", follow_redirects=True)
    assert "枠を使い切りました" in res.get_data(as_text=True)
    client.post("/services/release_creation/order")
    order = Order.query.one()
    webhook(_event("evt_1", order))
    assert client.post("/releases/new").status_code == 302
    assert Release.query.count() == 2


def test_production_requires_stripe_settings(monkeypatch):
    from app import create_app
    from app.config import ProductionConfig

    for name, value in (("SECRET_KEY", "x"), ("BASE_URL", "https://e.com"), ("TRUSTED_HOSTS", ["e.com"]),
                        ("MAIL_BACKEND", "smtp")):
        monkeypatch.setattr(ProductionConfig, name, value)
    monkeypatch.setattr(ProductionConfig, "STRIPE_SECRET_KEY", "")
    monkeypatch.setattr(ProductionConfig, "STRIPE_WEBHOOK_SECRET", "")
    with pytest.raises(RuntimeError, match="STRIPE"):
        create_app("production")


# ---- 記者発表会の企画・当日の同席（地域で料金が変わる） ----
@pytest.mark.parametrize("option,label,price", [
    ("iwate", "岩手県内", 80000),
    ("miyagi_aomori_akita", "宮城県・青森県・秋田県", 90000),
    ("fukushima_yamagata", "福島県・山形県", 100000),
    ("other", "その他", 120000),
])
def test_press_conference_price_depends_on_region(client, logged_in, gateway, option, label, price):
    res = client.post("/services/interview_planning/order", data={"option": option, "amount_yen": "1"})
    assert res.status_code == 303
    order = Order.query.one()
    assert order.amount_yen == price and order.service_option == option
    assert order.service_name == f"記者発表会の企画・当日の同席（{label}）"
    assert gateway["created"][0]["amount"] == price and label in gateway["created"][0]["name"]


@pytest.mark.parametrize("option", [None, "", "tokyo", "IWATE", "iwate ", "<script>", "other\n"])
def test_press_conference_requires_a_valid_region(client, logged_in, gateway, option):
    data = {} if option is None else {"option": option}
    res = client.post("/services/interview_planning/order", data=data, follow_redirects=True)
    assert "開催する地域を選んでください" in res.get_data(as_text=True)
    assert Order.query.count() == 0 and gateway["created"] == []


def test_region_is_ignored_for_services_without_options(client, logged_in, gateway):
    client.post("/services/article_creation/order", data={"option": "other"})
    order = Order.query.one()
    assert order.amount_yen == 10000 and order.service_option is None and order.service_name == "自社媒体などに掲載する記事制作"


def test_region_price_can_be_overridden_by_environment(client, logged_in, gateway, monkeypatch):
    monkeypatch.setenv("PRICE_INTERVIEW_PLANNING_IWATE", "70000")
    client.post("/services/interview_planning/order", data={"option": "iwate"})
    assert Order.query.one().amount_yen == 70000
    monkeypatch.setenv("PRICE_INTERVIEW_PLANNING_OTHER", "abc")
    from app import create_app

    with pytest.raises(RuntimeError, match="PRICE_INTERVIEW_PLANNING_OTHER"):
        create_app("testing")

