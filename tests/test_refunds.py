import logging
import threading
import time
import uuid

import pytest
from sqlalchemy import update

from app import create_app, payments
from app.extensions import db
from app.models import Order, OrderRefund, Release, User, utcnow
from app.orders import refunds
from app.releases import service as release_service

from .conftest import login, make_user
from .test_orders import _event, _mails, webhook  # noqa: F401
from .test_releases import FULL


class _Calls(list):
    """Stripe の返金 API に渡された内容の記録（list として、中身を直接見られる）。"""

    behaviour: dict


@pytest.fixture()
def stripe_calls(monkeypatch):
    """Stripe の返金 API の代わり。呼ばれた内容を記録する。"""
    calls = _Calls()
    calls.behaviour = {"fail": False, "status": "succeeded"}

    def create_refund(payment_intent_id, amount_yen, idempotency_key, metadata=None):
        calls.append({"pi": payment_intent_id, "amount": amount_yen, "key": idempotency_key, "metadata": metadata})
        if calls.behaviour["fail"]:
            raise payments.PaymentError("boom")
        return {"id": f"re_{len(calls)}", "status": calls.behaviour["status"]}

    monkeypatch.setattr(payments, "create_refund", create_refund)
    return calls


@pytest.fixture()
def admin_client(app, admin_user):
    c = app.test_client()
    login(c, "admin@example.com")
    return c


def _order(user, service_type="interview_planning", status="paid", progress="received", amount=60000, **kw):
    kw.setdefault("paid_at", utcnow())
    kw.setdefault("stripe_payment_intent_id", f"pi_{uuid.uuid4().hex}")
    o = Order(user_id=user.id if user else None, service_type=service_type, service_name="テスト",
              amount_yen=amount, status=status, progress=progress, **kw)
    db.session.add(o)
    db.session.commit()
    return o


def _refund(admin_client, order, amount, reason="policy", note=""):
    return admin_client.post(f"/admin/orders/{order.id}/refund",
                             data={"amount_yen": str(amount), "reason": reason, "note": note}, follow_redirects=True)


# ================= 返金の基準 =================
@pytest.mark.parametrize("progress,expected_total", [("received", 60000), ("in_progress", 30000), ("delivered", 0)])
def test_refund_limit_follows_progress(app, client_user, progress, expected_total):
    o = _order(client_user, progress=progress)
    limit = refunds.refund_limit(o)
    assert limit.total_yen == expected_total and limit.remaining_yen == expected_total


def test_half_is_rounded_down(app, client_user):
    o = _order(client_user, progress="in_progress", amount=10001)
    assert refunds.refund_limit(o).total_yen == 5000


def test_remaining_subtracts_previous_refunds(app, client_user):
    o = _order(client_user, progress="in_progress", refunded_amount=10000, status="partially_refunded")
    assert refunds.refund_limit(o).remaining_yen == 20000


def test_no_media_reason_allows_full_refund_only_for_interview_planning(app, client_user):
    delivered = _order(client_user, progress="delivered")
    assert refunds.refund_limit(delivered).total_yen == 0
    assert refunds.refund_limit(delivered, "no_media").total_yen == 60000
    other = _order(client_user, service_type="interview_accompany", progress="delivered", amount=50000)
    assert refunds.refund_limit(other, "no_media").total_yen == 0


# ================= 返金の実行 =================
def test_full_refund_when_received(admin_client, client_user, stripe_calls, app):
    o = _order(client_user)
    res = _refund(admin_client, o, 60000)
    assert "60,000円を返金しました" in res.get_data(as_text=True)
    db.session.refresh(o)
    assert o.status == "refunded" and o.refunded_amount == 60000
    assert stripe_calls[0]["pi"] == o.stripe_payment_intent_id and stripe_calls[0]["amount"] == 60000
    row = OrderRefund.query.one()
    assert (row.status, row.reason, row.stripe_refund_id) == ("succeeded", "policy", "re_1")
    assert stripe_calls[0]["key"] == f"refund-{o.id}-{row.id}"  # 注文番号も入れる（開発の DB を作り直しても、重ならない）
    mail = next(m for m in _mails(app) if m["to"] == "client@example.com")
    assert "返金のお知らせ" in mail["subject"] and "60,000円" in mail["body"]


def test_partial_then_remaining_refund(admin_client, client_user, stripe_calls):
    o = _order(client_user, progress="in_progress")
    _refund(admin_client, o, 10000)
    db.session.refresh(o)
    assert o.status == "partially_refunded" and o.refunded_amount == 10000
    _refund(admin_client, o, 20000)
    db.session.refresh(o)
    assert o.refunded_amount == 30000 and o.status == "partially_refunded"  # 着手の上限（50%）まで
    res = _refund(admin_client, o, 1)
    assert "あと0円" in res.get_data(as_text=True)
    assert len(stripe_calls) == 2
    assert stripe_calls[0]["key"] != stripe_calls[1]["key"]  # 識別キーは、毎回別


def test_cannot_refund_more_than_the_rule_allows(admin_client, client_user, stripe_calls):
    o = _order(client_user, progress="in_progress")
    res = _refund(admin_client, o, 30001)
    assert "あと30,000円まで" in res.get_data(as_text=True)
    db.session.refresh(o)
    assert o.refunded_amount == 0 and stripe_calls == [] and OrderRefund.query.count() == 0


def test_cannot_refund_delivered_order(admin_client, client_user, stripe_calls):
    o = _order(client_user, progress="delivered")
    res = _refund(admin_client, o, 1000)
    assert "返金できません" in res.get_data(as_text=True)
    assert stripe_calls == []


def test_no_media_reason_refunds_everything_even_if_delivered(admin_client, client_user, stripe_calls):
    o = _order(client_user, progress="delivered")
    _refund(admin_client, o, 60000, reason="no_media", note="当日の取材は0件でした")
    db.session.refresh(o)
    assert o.status == "refunded" and o.refund_reason == "当日の取材は0件でした"
    assert OrderRefund.query.one().reason == "no_media"


def test_no_media_reason_is_refused_for_other_services(admin_client, client_user, stripe_calls):
    o = _order(client_user, service_type="distribution_proxy", progress="delivered", amount=10000)
    res = _refund(admin_client, o, 10000, reason="no_media")
    assert "企画支援だけ" in res.get_data(as_text=True)
    assert stripe_calls == []


@pytest.mark.parametrize("amount", ["0", "-5", "abc", "", "1.5", "10" * 6])
def test_bad_refund_amounts_are_refused(admin_client, client_user, stripe_calls, amount):
    o = _order(client_user)
    admin_client.post(f"/admin/orders/{o.id}/refund", data={"amount_yen": amount, "reason": "policy"})
    assert stripe_calls == []
    db.session.refresh(o)
    assert o.refunded_amount == 0


def test_unknown_reason_is_refused(admin_client, client_user, stripe_calls):
    o = _order(client_user)
    res = _refund(admin_client, o, 1000, reason="external")  # 外部の返金の理由は、画面からは使えない
    assert "返金の理由が正しくありません" in res.get_data(as_text=True)
    assert stripe_calls == []


def test_long_note_is_refused(admin_client, client_user, stripe_calls):
    o = _order(client_user)
    res = _refund(admin_client, o, 1000, note="あ" * 501)
    assert "500文字以内" in res.get_data(as_text=True)
    assert stripe_calls == []


@pytest.mark.parametrize("status", ["unpaid", "canceled", "refunded"])
def test_only_paid_orders_can_be_refunded(admin_client, client_user, stripe_calls, status):
    o = _order(client_user, status=status, progress=None)
    _refund(admin_client, o, 1000)
    assert stripe_calls == []


def test_order_without_stripe_payment_cannot_be_refunded(admin_client, client_user, stripe_calls):
    o = _order(client_user)
    o.stripe_payment_intent_id = None
    db.session.commit()
    res = _refund(admin_client, o, 1000)
    assert "Stripe の支払いの記録がありません" in res.get_data(as_text=True)


def test_refund_needs_admin(client, client_user, stripe_calls):
    o = _order(client_user)
    login(client, "client@example.com")
    assert client.post(f"/admin/orders/{o.id}/refund", data={"amount_yen": "1000"}).status_code == 403
    assert stripe_calls == []


def test_stripe_failure_rolls_back_reservation(admin_client, client_user, stripe_calls):
    stripe_calls.behaviour["fail"] = True
    o = _order(client_user)
    res = _refund(admin_client, o, 60000)
    assert "Stripe での返金に失敗しました" in res.get_data(as_text=True)
    db.session.refresh(o)
    assert o.refunded_amount == 0 and o.status == "paid"
    assert OrderRefund.query.one().status == "failed"
    stripe_calls.behaviour["fail"] = False
    _refund(admin_client, o, 60000)  # やり直せる
    db.session.refresh(o)
    assert o.status == "refunded" and o.refunded_amount == 60000
    assert [r.status for r in OrderRefund.query.order_by(OrderRefund.id)] == ["failed", "succeeded"]


def test_stripe_reporting_failed_status_is_a_failure(admin_client, client_user, stripe_calls):
    stripe_calls.behaviour["status"] = "failed"
    o = _order(client_user)
    _refund(admin_client, o, 1000)
    db.session.refresh(o)
    assert o.refunded_amount == 0 and OrderRefund.query.one().status == "failed"


def test_failed_refund_sends_no_email(app, admin_client, client_user, stripe_calls):
    stripe_calls.behaviour["fail"] = True
    o = _order(client_user)
    _refund(admin_client, o, 1000)
    assert not [m for m in _mails(app) if m["to"] == "client@example.com"]


def test_refund_succeeds_even_if_mail_fails(app, admin_client, client_user, stripe_calls):
    app.config["MAIL_BACKEND"] = "not-implemented"
    o = _order(client_user)
    _refund(admin_client, o, 60000)
    db.session.refresh(o)
    assert o.status == "refunded"


def test_progress_change_between_page_load_and_refund_is_caught(admin_client, client_user, stripe_calls, monkeypatch):
    """画面を開いたあとに、進捗が「納品済み」になったら、古い上限では返金できない。"""
    o = _order(client_user)
    original = refunds.refund_limit

    def limit_then_deliver(order, reason="policy"):
        result = original(order, reason)
        # 別のリクエスト（別の画面）で納品された状況を作る。この画面が持っている値（order.progress）は、
        # 古いまま（commit すると、値が読み直されてしまうので、commit はしない）
        db.session.execute(
            update(Order).where(Order.id == order.id).values(progress="delivered")
            .execution_options(synchronize_session=False)
        )
        return result

    monkeypatch.setattr(refunds, "refund_limit", limit_then_deliver)
    res = _refund(admin_client, o, 60000)
    assert "同時に別の操作があった" in res.get_data(as_text=True)
    assert stripe_calls == []
    db.session.refresh(o)
    assert o.refunded_amount == 0


def test_refund_page_shows_rule_and_history(admin_client, client_user, stripe_calls):
    o = _order(client_user, progress="in_progress")
    _refund(admin_client, o, 10000, note="部分返金のテスト")
    body = admin_client.get(f"/admin/orders/{o.id}").get_data(as_text=True)
    for expected in ("進捗が「着手」なので、50%を返金できます", "あと 20,000円", "部分返金のテスト", "成功"):
        assert expected in body, expected


def test_interview_planning_page_offers_no_media_option(admin_client, client_user):
    o = _order(client_user, progress="delivered")
    body = admin_client.get(f"/admin/orders/{o.id}").get_data(as_text=True)
    assert "当日、取材するメディアが一件もなかった" in body and "全額（あと 60,000円）" in body


def test_client_sees_refunded_amount(client, client_user, stripe_calls, admin_client):
    o = _order(client_user, progress="in_progress")
    _refund(admin_client, o, 15000)
    login(client, "client@example.com")
    body = client.get(f"/mypage/orders/{o.id}").get_data(as_text=True)
    assert "返金額：15,000円" in body and "一部返金" in body


# ================= リリース作成の追加（枠）の返金 =================
def _used_free_slot(user):
    db.session.add(Release(user_id=user.id, first_submitted_at=utcnow(), **FULL))
    db.session.commit()


def _slot_order(user):
    return _order(user, service_type="release_creation", progress=None, amount=5000)


def test_unused_slot_can_be_refunded_in_full(admin_client, client_user, stripe_calls):
    _used_free_slot(client_user)
    o = _slot_order(client_user)
    assert not release_service.can_create(client_user) or True
    assert release_service.can_create(client_user)  # 購入した枠で、もう1件作れる
    _refund(admin_client, o, 5000)
    db.session.refresh(o)
    assert o.status == "refunded"
    assert not release_service.can_create(client_user)  # 枠が減る
    assert OrderRefund.query.one().reason == "slot_unused"


def test_used_slot_cannot_be_refunded(admin_client, client_user, stripe_calls):
    _used_free_slot(client_user)
    o = _slot_order(client_user)
    db.session.add(Release(user_id=client_user.id, first_submitted_at=utcnow(), **FULL))  # 購入した枠を使った
    db.session.commit()
    res = _refund(admin_client, o, 5000)
    assert "すでに書類の提出に使った枠は、返金できません" in res.get_data(as_text=True)
    assert stripe_calls == []


def test_one_of_two_slots_can_be_refunded_if_only_one_is_used(admin_client, client_user, stripe_calls):
    _used_free_slot(client_user)
    a, b = _slot_order(client_user), _slot_order(client_user)
    db.session.add(Release(user_id=client_user.id, first_submitted_at=utcnow(), **FULL))  # 購入した枠を1つ使った
    db.session.commit()
    _refund(admin_client, a, 5000)
    db.session.refresh(a)
    assert a.status == "refunded"
    res = _refund(admin_client, b, 5000)  # 残り1つは使用済みを賄うので、返金できない
    assert "返金できません" in res.get_data(as_text=True)
    db.session.refresh(b)
    assert b.status == "paid"


def test_slot_refund_must_be_full_amount(admin_client, client_user, stripe_calls):
    _used_free_slot(client_user)
    o = _slot_order(client_user)
    res = _refund(admin_client, o, 1000)
    assert "全額" in res.get_data(as_text=True)
    assert stripe_calls == []


def test_slot_order_page_has_no_progress_but_has_refund(admin_client, client_user):
    _used_free_slot(client_user)
    o = _slot_order(client_user)
    body = admin_client.get(f"/admin/orders/{o.id}").get_data(as_text=True)
    assert "使っていない枠は、全額を返金できます" in body and "納期・進捗・納品はありません" in body


# ================= Stripe からの通知（返金・異議申し立て） =================
def _charge_event(event_id, order, amount_refunded, event_type="charge.refunded"):
    return {"id": event_id, "type": event_type, "data": {"object": {
        "id": "ch_1", "payment_intent": order.stripe_payment_intent_id, "amount": order.amount_yen,
        "amount_refunded": amount_refunded}}}


def test_dashboard_refund_is_reflected_by_notification(app, client_user, webhook):
    db.session.add(User(email="boss@example.com", password_hash="x", role="admin"))
    o = _order(client_user, progress="in_progress")
    db.session.commit()
    assert webhook(_charge_event("evt_1", o, 20000)).status_code == 200
    db.session.refresh(o)
    assert o.refunded_amount == 20000 and o.status == "partially_refunded"
    row = OrderRefund.query.one()
    assert (row.reason, row.status, row.amount_yen) == ("external", "succeeded", 20000)
    assert [m for m in _mails(app) if m["to"] == "boss@example.com"]


def test_full_dashboard_refund_removes_purchased_slot(app, client_user, webhook):
    _used_free_slot(client_user)
    o = _slot_order(client_user)
    assert release_service.can_create(client_user)
    webhook(_charge_event("evt_1", o, 5000))
    db.session.refresh(o)
    assert o.status == "refunded" and not release_service.can_create(client_user)


def test_refund_made_by_app_is_not_counted_twice(client_user, webhook, admin_client, stripe_calls):
    o = _order(client_user)
    _refund(admin_client, o, 60000)
    webhook(_charge_event("evt_1", o, 60000))  # アプリからの返金の通知
    db.session.refresh(o)
    assert o.refunded_amount == 60000 and OrderRefund.query.count() == 1


def test_notification_before_app_finishes_confirms_pending_refund(client_user, webhook):
    o = _order(client_user, progress="in_progress", refunded_amount=10000)
    # Stripe が受け付けた（Stripe の返金 ID がある）が、まだ処理中の返金
    db.session.add(OrderRefund(order_id=o.id, amount_yen=10000, reason="policy", status="pending",
                               stripe_refund_id="re_accepted"))
    db.session.commit()
    webhook(_charge_event("evt_1", o, 10000))
    db.session.refresh(o)
    assert OrderRefund.query.one().status == "succeeded" and o.status == "partially_refunded"
    assert o.refunded_amount == 10000


def test_stale_notification_is_ignored(client_user, webhook):
    o = _order(client_user, progress="in_progress", refunded_amount=10000, status="partially_refunded")
    db.session.add(OrderRefund(order_id=o.id, amount_yen=10000, reason="policy", status="succeeded"))
    db.session.commit()
    webhook(_charge_event("evt_1", o, 0))
    db.session.refresh(o)
    assert o.refunded_amount == 10000


def test_refund_notification_for_unknown_payment_is_acknowledged(client_user, webhook, caplog):
    o = _order(client_user)
    event = _charge_event("evt_1", o, 100)
    event["data"]["object"]["payment_intent"] = "pi_unknown"
    with caplog.at_level(logging.ERROR):
        assert webhook(event).status_code == 200
    assert "対応する注文がありません" in caplog.text


def test_refund_notification_duplicate_is_processed_once(client_user, webhook):
    o = _order(client_user, progress="in_progress")
    event = _charge_event("evt_1", o, 10000)
    webhook(event)
    webhook(event)
    assert OrderRefund.query.count() == 1


def test_external_refund_cannot_exceed_order_amount(client_user, webhook):
    o = _order(client_user, amount=1000, progress="received")
    webhook(_charge_event("evt_1", o, 999999))
    db.session.refresh(o)
    assert o.refunded_amount == 1000 and o.status == "refunded"


def test_dispute_marks_order_and_notifies_admins(app, client_user, webhook, caplog):
    db.session.add(User(email="boss@example.com", password_hash="x", role="admin"))
    o = _order(client_user)
    db.session.commit()
    event = {"id": "evt_d", "type": "charge.dispute.created",
             "data": {"object": {"id": "dp_1", "payment_intent": o.stripe_payment_intent_id}}}
    with caplog.at_level(logging.ERROR):
        assert webhook(event).status_code == 200
    db.session.refresh(o)
    assert o.disputed_at is not None and o.status == "paid"  # 自動では何も変えない
    assert "異議申し立て" in caplog.text
    mail = next(m for m in _mails(app) if m["to"] == "boss@example.com")
    assert "要対応" in mail["subject"] and f"/admin/orders/{o.id}" in mail["body"]


def test_dispute_is_reported_only_once(app, client_user, webhook):
    db.session.add(User(email="boss@example.com", password_hash="x", role="admin"))
    o = _order(client_user)
    db.session.commit()
    for i in (1, 2):
        webhook({"id": f"evt_{i}", "type": "charge.dispute.created",
                 "data": {"object": {"payment_intent": o.stripe_payment_intent_id}}})
    assert len([m for m in _mails(app) if m["to"] == "boss@example.com"]) == 1


def test_dispute_shown_in_admin_pages(admin_client, client_user):
    o = _order(client_user, disputed_at=utcnow())
    assert "異議申し立て" in admin_client.get(f"/admin/orders/{o.id}").get_data(as_text=True)
    assert "異議申し立て" in admin_client.get("/admin/orders").get_data(as_text=True)


# ================= 同時の返金（別のスレッド） =================
@pytest.fixture()
def file_app(tmp_path, monkeypatch):
    from app.config import TestingConfig

    monkeypatch.setattr(TestingConfig, "SQLALCHEMY_DATABASE_URI", f"sqlite:///{tmp_path}/refund_race.db")
    app = create_app("testing")
    app.config["UPLOAD_DIR"] = str(tmp_path / "uploads")
    with app.app_context():
        db.create_all()
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()


def test_two_simultaneous_refunds_never_exceed_the_limit(file_app, monkeypatch):
    sent = []
    barrier = threading.Barrier(2)

    def slow_refund(payment_intent_id, amount_yen, idempotency_key, metadata=None):
        sent.append(amount_yen)
        return {"id": f"re_{idempotency_key}", "status": "succeeded"}

    monkeypatch.setattr(payments, "create_refund", slow_refund)
    with file_app.app_context():
        user = make_user(email="r@example.com")
        admin = make_user(email="a@example.com", role="admin")
        order = _order(user, progress="received", amount=60000)
        order_id, admin_id = order.id, admin.id

    # 2つの処理が、どちらも「まだ60,000円返金できる」と読んでから、確保に進むよう、隙間を広げる
    original_limit = refunds.refund_limit

    def slow_limit(order, reason="policy"):
        result = original_limit(order, reason)
        time.sleep(0.4)
        return result

    monkeypatch.setattr(refunds, "refund_limit", slow_limit)
    results = []

    def run():
        with file_app.app_context():
            try:
                barrier.wait()
                refunds.create_refund(db.session.get(Order, order_id), db.session.get(User, admin_id), 40000, "policy")
                results.append("ok")
            except refunds.RefundError as e:
                results.append("error")
            finally:
                db.session.remove()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(results) == ["error", "ok"], results  # 40,000円を2回は通らない（合計 80,000円 > 60,000円）
    with file_app.app_context():
        assert db.session.get(Order, order_id).refunded_amount == 40000
    assert sent == [40000]
