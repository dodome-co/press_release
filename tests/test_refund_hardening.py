"""段階4のセキュリティ点検（返金の安全性・Webhook・退会など）への対策のテスト。"""
import io
import logging
from datetime import timedelta
from unittest.mock import patch

import pytest
import stripe
from PIL import Image

from app import payments
from app.extensions import db
from app.models import Order, OrderRefund, Release, User, utcnow
from app.orders import refunds
from app.releases import service as release_service
from app.uploads import delete_directory, resolve_path

from .conftest import login, make_user
from .test_orders import _mails, webhook  # noqa: F401
from .test_refunds import (  # noqa: F401
    _charge_event,
    _order,
    _refund,
    _slot_order,
    _used_free_slot,
    admin_client,
    stripe_calls,
)
from .test_releases import FULL


def _submit_attempt(user):
    """下書きを1件作って、初めての提出を試みる。通れば True。"""
    draft = Release(user_id=user.id, **FULL)
    db.session.add(draft)
    db.session.commit()
    try:
        release_service.submit(draft)
        return True
    except release_service.ReleaseError:
        db.session.rollback()
        return False


# ================= 中1：返金の途中で、返金する枠が使えない =================
def test_slot_being_refunded_cannot_be_used_meanwhile(admin_client, client_user, monkeypatch):
    _used_free_slot(client_user)
    order = _slot_order(client_user)
    seen = {}

    def stripe_refund(payment_intent_id, amount_yen, idempotency_key, metadata=None):
        seen["during"] = _submit_attempt(client_user)  # Stripe の応答を待っている間に、提出しようとする
        return {"id": "re_1", "status": "succeeded"}

    monkeypatch.setattr(payments, "create_refund", stripe_refund)
    _refund(admin_client, order, 5000)
    assert seen["during"] is False  # 返金する枠は、使えない
    db.session.refresh(order)
    assert order.status == "refunded" and release_service.count_first_submitted(client_user.id) == 1


def test_slot_counts_again_if_refund_is_rejected(admin_client, client_user, monkeypatch):
    _used_free_slot(client_user)
    order = _slot_order(client_user)

    def rejected(*args, **kwargs):
        raise payments.PaymentRejected("no")

    monkeypatch.setattr(payments, "create_refund", rejected)
    _refund(admin_client, order, 5000)
    db.session.refresh(order)
    assert order.status == "paid" and order.refunded_amount == 0
    assert release_service.can_create(client_user)  # 返金が失敗すれば、枠は、また使える


def test_slot_stays_reserved_while_refund_outcome_is_unknown(admin_client, client_user, monkeypatch):
    _used_free_slot(client_user)
    order = _slot_order(client_user)

    def uncertain(*args, **kwargs):
        raise payments.PaymentUncertain("timeout")

    monkeypatch.setattr(payments, "create_refund", uncertain)
    _refund(admin_client, order, 5000)
    assert not release_service.can_create(client_user)  # 結果が分かるまで、枠は使えない


# ================= 中2：通信エラーで、二重に返金しない =================
def test_uncertain_failure_keeps_reservation_so_retry_cannot_double_refund(admin_client, client_user, monkeypatch):
    order = _order(client_user, progress="in_progress")  # 上限 30,000円
    calls = []

    def uncertain(payment_intent_id, amount_yen, idempotency_key, metadata=None):
        calls.append(amount_yen)
        raise payments.PaymentUncertain("timeout")

    monkeypatch.setattr(payments, "create_refund", uncertain)
    res = _refund(admin_client, order, 30000)
    assert "応答を確認できませんでした" in res.get_data(as_text=True)
    db.session.refresh(order)
    assert order.refunded_amount == 30000  # 確保は、そのまま
    row = OrderRefund.query.one()
    assert row.status == "pending"

    res = _refund(admin_client, order, 30000)  # もう一度押しても、
    assert "あと0円" in res.get_data(as_text=True)
    assert calls == [30000]  # Stripe には、1回しか送られない


def test_stripe_error_classification(app, monkeypatch):
    app.config["STRIPE_SECRET_KEY"] = "sk_test_x"

    def raising(error):
        def create(**kwargs):
            raise error
        return create

    cases = [
        (stripe.InvalidRequestError("bad", "param"), payments.PaymentRejected),
        (stripe.AuthenticationError("auth"), payments.PaymentRejected),
        (stripe.PermissionError("perm"), payments.PaymentRejected),
        (stripe.RateLimitError("rate"), payments.PaymentRejected),
        (stripe.APIConnectionError("conn"), payments.PaymentUncertain),
        (stripe.APIError("5xx"), payments.PaymentUncertain),
        (stripe.StripeError("other"), payments.PaymentUncertain),
    ]
    for error, expected in cases:
        monkeypatch.setattr(stripe.Refund, "create", raising(error))
        with pytest.raises(expected):
            payments.create_refund("pi_1", 100, "key")


def test_resolve_pending_finds_refund_that_actually_went_through(admin_client, client_user, monkeypatch):
    order = _order(client_user, progress="in_progress")
    monkeypatch.setattr(payments, "create_refund", lambda *a, **k: (_ for _ in ()).throw(payments.PaymentUncertain("x")))
    _refund(admin_client, order, 20000)
    row = OrderRefund.query.one()
    row.created_at = utcnow() - timedelta(minutes=30)
    db.session.commit()
    monkeypatch.setattr(payments, "find_refund", lambda pi, rid: {"id": "re_found", "status": "succeeded"})
    res = admin_client.post(f"/admin/orders/{order.id}/resolve-refunds", follow_redirects=True)
    assert "成功 1件" in res.get_data(as_text=True)
    db.session.refresh(order)
    db.session.refresh(row)
    assert row.status == "succeeded" and row.stripe_refund_id == "re_found"
    assert order.refunded_amount == 20000 and order.status == "partially_refunded"


def test_resolve_pending_releases_refund_that_never_reached_stripe(admin_client, client_user, monkeypatch):
    order = _order(client_user, progress="in_progress")
    monkeypatch.setattr(payments, "create_refund", lambda *a, **k: (_ for _ in ()).throw(payments.PaymentUncertain("x")))
    _refund(admin_client, order, 20000)
    row = OrderRefund.query.one()
    row.created_at = utcnow() - timedelta(minutes=30)
    db.session.commit()
    monkeypatch.setattr(payments, "find_refund", lambda pi, rid: None)
    res = admin_client.post(f"/admin/orders/{order.id}/resolve-refunds", follow_redirects=True)
    assert "失敗（返金額を戻しました）1件" in res.get_data(as_text=True)
    db.session.refresh(order)
    db.session.refresh(row)
    assert row.status == "failed" and order.refunded_amount == 0 and order.status == "paid"


def test_resolve_pending_skips_recent_ones(admin_client, client_user, monkeypatch):
    order = _order(client_user, progress="in_progress")
    monkeypatch.setattr(payments, "create_refund", lambda *a, **k: (_ for _ in ()).throw(payments.PaymentUncertain("x")))
    _refund(admin_client, order, 20000)
    asked = []
    monkeypatch.setattr(payments, "find_refund", lambda pi, rid: asked.append(rid))
    res = admin_client.post(f"/admin/orders/{order.id}/resolve-refunds", follow_redirects=True)
    assert "作ってから間もないため、確認しなかったもの 1件" in res.get_data(as_text=True)
    assert asked == []
    db.session.refresh(order)
    assert order.refunded_amount == 20000


def test_resolve_pending_when_stripe_cannot_be_reached(admin_client, client_user, monkeypatch):
    order = _order(client_user, progress="in_progress")
    monkeypatch.setattr(payments, "create_refund", lambda *a, **k: (_ for _ in ()).throw(payments.PaymentUncertain("x")))
    _refund(admin_client, order, 20000)
    OrderRefund.query.update({"created_at": utcnow() - timedelta(minutes=30)})
    db.session.commit()
    monkeypatch.setattr(payments, "find_refund", lambda pi, rid: (_ for _ in ()).throw(payments.PaymentUncertain("x")))
    res = admin_client.post(f"/admin/orders/{order.id}/resolve-refunds", follow_redirects=True)
    assert "Stripe に問い合わせられませんでした" in res.get_data(as_text=True)
    db.session.refresh(order)
    assert order.refunded_amount == 20000 and OrderRefund.query.one().status == "pending"  # 変えない


def test_resolve_button_only_shown_when_pending_exists(admin_client, client_user, monkeypatch):
    order = _order(client_user, progress="in_progress")
    assert "処理中の返金を確認する" not in admin_client.get(f"/admin/orders/{order.id}").get_data(as_text=True)
    db.session.add(OrderRefund(order_id=order.id, amount_yen=100, reason="policy", status="pending"))
    db.session.commit()
    assert "処理中の返金を確認する" in admin_client.get(f"/admin/orders/{order.id}").get_data(as_text=True)


def test_find_refund_works_with_real_stripe_objects(app, monkeypatch):
    """Stripe の本物のオブジェクト（.get() が使えない）を、正しく読めること。"""
    app.config["STRIPE_SECRET_KEY"] = "sk_test_x"
    refunds_list = [
        stripe.Refund.construct_from({"id": "re_other", "status": "succeeded", "metadata": {}}, "k"),
        stripe.Refund.construct_from({"id": "re_no_meta", "status": "succeeded"}, "k"),
        stripe.Refund.construct_from({"id": "re_mine", "status": "succeeded", "metadata": {"refund_id": "7"}}, "k"),
    ]

    class Listing:
        def auto_paging_iter(self):
            return iter(refunds_list)

    monkeypatch.setattr(stripe.Refund, "list", lambda **kw: Listing())
    assert payments.find_refund("pi_1", 7) == {"id": "re_mine", "status": "succeeded"}
    assert payments.find_refund("pi_1", 8) is None


def test_find_refund_wraps_stripe_errors(app, monkeypatch):
    app.config["STRIPE_SECRET_KEY"] = "sk_test_x"
    monkeypatch.setattr(stripe.Refund, "list", lambda **kw: (_ for _ in ()).throw(stripe.APIConnectionError("x")))
    with pytest.raises(payments.PaymentUncertain):
        payments.find_refund("pi_1", 1)


# ================= 低1：返金の状態 =================
@pytest.mark.parametrize("status", ["failed", "canceled", "requires_action", "weird"])
def test_unsuccessful_stripe_status_is_a_failure(admin_client, client_user, stripe_calls, status):
    stripe_calls.behaviour["status"] = status
    order = _order(client_user)
    _refund(admin_client, order, 1000)
    db.session.refresh(order)
    assert order.refunded_amount == 0 and order.status == "paid" and OrderRefund.query.one().status == "failed"


def test_pending_status_from_stripe_is_accepted_but_kept_pending(admin_client, client_user, stripe_calls):
    stripe_calls.behaviour["status"] = "pending"
    order = _order(client_user)
    _refund(admin_client, order, 60000)
    db.session.refresh(order)
    row = OrderRefund.query.one()
    assert row.status == "pending" and row.stripe_refund_id == "re_1"
    assert order.refunded_amount == 60000 and order.status == "refunded"


def _refund_event(event_id, refund_id, status, event_type="refund.updated"):
    return {"id": event_id, "type": event_type, "data": {"object": {"id": refund_id, "status": status}}}


def test_refund_updated_to_succeeded_confirms_pending(client_user, webhook, admin_client, stripe_calls):
    stripe_calls.behaviour["status"] = "pending"
    order = _order(client_user)
    _refund(admin_client, order, 60000)
    assert webhook(_refund_event("evt_1", "re_1", "succeeded")).status_code == 200
    assert OrderRefund.query.one().status == "succeeded"


def test_refund_failed_later_gives_the_amount_back_and_alerts_admins(app, client_user, webhook, admin_client, stripe_calls):
    stripe_calls.behaviour["status"] = "pending"
    order = _order(client_user)
    _refund(admin_client, order, 60000)
    for event_type in ("refund.failed",):
        assert webhook(_refund_event("evt_1", "re_1", "failed", event_type)).status_code == 200
    db.session.refresh(order)
    assert OrderRefund.query.one().status == "failed"
    assert order.refunded_amount == 0 and order.status == "paid"
    mail = next(m for m in _mails(app) if "返金が失敗しました" in m["subject"])
    assert f"注文番号：{order.id}" in mail["body"]


def test_succeeded_refund_that_later_fails_is_reverted(app, client_user, webhook, admin_client, stripe_calls):
    order = _order(client_user)
    _refund(admin_client, order, 60000)
    assert OrderRefund.query.one().status == "succeeded"
    webhook(_refund_event("evt_1", "re_1", "canceled", "charge.refund.updated"))
    db.session.refresh(order)
    assert OrderRefund.query.one().status == "failed" and order.refunded_amount == 0 and order.status == "paid"


def test_refund_update_for_unknown_refund_is_ignored(client_user, webhook):
    assert webhook(_refund_event("evt_1", "re_unknown", "failed")).status_code == 200


# ================= 低2：処理中の返金を、別の通知で、誤って確定しない =================
def test_external_refund_notification_does_not_confirm_unreached_pending(client_user, webhook):
    order = _order(client_user, progress="in_progress", refunded_amount=10000)
    db.session.add(OrderRefund(order_id=order.id, amount_yen=10000, reason="policy", status="pending"))  # Stripe の ID なし
    db.session.commit()
    webhook(_charge_event("evt_1", order, 10000))
    assert OrderRefund.query.one().status == "pending"  # 確定しない（resolve_pending で確かめる）


# ================= 低3：注文に結びつかない通知は、管理者に知らせる =================
def test_unmatched_dispute_alerts_admins(app, client_user, webhook):
    db.session.add(User(email="boss@example.com", password_hash="x", role="admin"))
    db.session.commit()
    event = {"id": "evt_1", "type": "charge.dispute.created", "data": {"object": {"payment_intent": "pi_unknown"}}}
    assert webhook(event).status_code == 200
    mail = next(m for m in _mails(app) if m["to"] == "boss@example.com")
    assert "注文に結びつかない" in mail["subject"] and "pi_unknown" in mail["body"]


def test_unmatched_refund_alerts_admins(app, client_user, webhook):
    db.session.add(User(email="boss@example.com", password_hash="x", role="admin"))
    order = _order(client_user)
    db.session.commit()
    event = _charge_event("evt_1", order, 100)
    event["data"]["object"]["payment_intent"] = "pi_unknown"
    webhook(event)
    assert [m for m in _mails(app) if m["to"] == "boss@example.com" and "pi_unknown" in m["body"]]


# ================= 低4：異議申し立て中は、返金できない =================
def test_disputed_order_cannot_be_refunded(admin_client, client_user, stripe_calls):
    order = _order(client_user, disputed_at=utcnow())
    res = _refund(admin_client, order, 1000)
    assert "異議申し立て中の注文は" in res.get_data(as_text=True)
    assert stripe_calls == []
    db.session.refresh(order)
    assert order.refunded_amount == 0


def test_dispute_arriving_during_refund_is_caught_by_reservation(admin_client, client_user, stripe_calls, monkeypatch):
    from sqlalchemy import update

    order = _order(client_user)
    original = refunds.refund_limit

    def limit_then_dispute(o, reason="policy"):
        result = original(o, reason)
        db.session.execute(update(Order).where(Order.id == o.id).values(disputed_at=utcnow())
                           .execution_options(synchronize_session=False))
        return result

    monkeypatch.setattr(refunds, "refund_limit", limit_then_dispute)
    res = _refund(admin_client, order, 1000)
    assert "同時に別の操作があった" in res.get_data(as_text=True)
    assert stripe_calls == []


# ================= 低5：全角・上付きの数字で 500 にならない =================
@pytest.mark.parametrize("value", ["²", "１０００", "①", "٣"])
def test_non_ascii_digits_do_not_cause_500(admin_client, client_user, stripe_calls, value):
    order = _order(client_user)
    assert admin_client.post(f"/admin/orders/{order.id}/refund", data={"amount_yen": value, "reason": "policy"}).status_code == 302
    assert admin_client.post(f"/admin/orders/{order.id}/revisions", data={"free_revisions_used": value}).status_code == 302
    assert stripe_calls == []


# ================= 低8：ファイル削除の失敗が分かる =================
def test_delete_directory_reports_failure(app):
    (resolve_path("users/9")).mkdir(parents=True, exist_ok=True)
    with patch("shutil.rmtree", lambda *a, **k: None):
        assert delete_directory("users/9") is False
    assert delete_directory("users/9") is True
    assert delete_directory("users/9") is True  # もう無い


def test_delete_directory_refuses_outside_upload_dir(app, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    assert delete_directory("../outside") is False
    assert outside.exists()


def test_leftover_directory_is_logged_on_account_deletion(app, admin_client, caplog):
    from .test_accounts import _delete, _full_client

    user, *_ = _full_client()
    with patch("shutil.rmtree", lambda *a, **k: None), caplog.at_level(logging.WARNING):
        _delete(admin_client, user)
    assert "フォルダを削除できませんでした" in caplog.text


# ================= 低9：番号の再利用で、古いセッションが別人にならない =================
def test_reused_user_id_does_not_inherit_old_session(app, client):
    if not str(db.engine.url).startswith("sqlite"):
        pytest.skip("番号を再利用するのは SQLite だけ（MySQL は、消した番号を再利用しない）")
    from .conftest import login as do_login

    first = make_user(email="first@example.com")
    do_login(client, "first@example.com")
    assert client.get("/mypage/").status_code == 200
    old_id = first.id
    db.session.delete(first)
    db.session.commit()
    second = make_user(email="second@example.com")  # SQLite は、最大の番号を再利用しうる
    assert second.id == old_id
    assert client.get("/mypage/").status_code == 302  # 古いセッションで、別人としてログインしない


# ================= 低11：識別キー =================
def test_refund_idempotency_key_includes_order_id(admin_client, client_user, stripe_calls):
    order = _order(client_user)
    _refund(admin_client, order, 1000)
    assert stripe_calls[0]["key"] == f"refund-{order.id}-1"
