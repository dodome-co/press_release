import pytest
from sqlalchemy.exc import IntegrityError

from app import services_catalog as catalog
from app.extensions import db
from app.models import Order, Release, StripeEvent, User, utcnow
from app.releases import service

from .conftest import make_user
from .test_releases import FULL


def _order(user, service_type="release_creation", status="unpaid", amount=5000, **kw):
    o = Order(user_id=user.id if user else None, service_type=service_type, service_name="テスト",
              amount_yen=amount, status=status, **kw)
    db.session.add(o)
    db.session.commit()
    return o


# ---- サービスの一覧 ----
def test_five_services_with_provisional_prices(app):
    keys = [s.key for s in catalog.all_services()]
    assert keys == ["release_creation", "interview_planning", "distribution_proxy", "article_creation", "video_creation"]
    assert "interview_accompany" not in keys  # 記者発表会の企画・当日の同席に、統合した
    prices = {s.key: s.all_prices() for s in catalog.all_services()}
    assert prices == {
        "release_creation": [4_000],
        "interview_planning": [80_000, 90_000, 100_000, 120_000],
        "distribution_proxy": [3_000],
        "article_creation": [10_000],
        "video_creation": [30_000],
    }


def test_service_names(app):
    names = {s.key: s.name for s in catalog.all_services()}
    assert names == {
        "release_creation": "2件目以降のリリース作成（1件分）",
        "interview_planning": "記者発表会の企画・当日の同席",
        "distribution_proxy": "投げ込み交渉代行",
        "article_creation": "自社媒体などに掲載する記事制作",
        "video_creation": "自社媒体などに掲載する動画制作",
    }
    assert "岩手県内の場合は直接持ち込みます" in catalog.get_service("distribution_proxy").description


def test_regional_options(app):
    service = catalog.get_service("interview_planning")
    assert [(o.key, o.label) for o in service.options] == [
        ("iwate", "岩手県内"), ("miyagi_aomori_akita", "宮城県・青森県・秋田県"),
        ("fukushima_yamagata", "福島県・山形県"), ("other", "その他")]
    assert service.price_for("iwate") == 80_000 and service.price_for("other") == 120_000
    assert service.price_for(None) is None and service.price_for("tokyo") is None  # 地域が選ばれていない


def test_price_can_be_overridden_by_environment(app, monkeypatch):
    monkeypatch.setenv("PRICE_VIDEO_CREATION", "40000")
    assert catalog.get_service("video_creation").price_yen == 40_000
    assert catalog.get_service("article_creation").price_yen == 10_000
    monkeypatch.setenv("PRICE_INTERVIEW_PLANNING_FUKUSHIMA_YAMAGATA", "110000")
    assert catalog.get_service("interview_planning").price_for("fukushima_yamagata") == 110_000


def test_only_video_needs_materials_and_only_release_creation_has_no_delivery(app):
    assert [s.key for s in catalog.all_services() if s.needs_materials_url] == ["video_creation"]
    assert [s.key for s in catalog.all_services() if not s.has_delivery] == ["release_creation"]


def test_only_release_creation_skips_the_interview(app):
    assert [s.key for s in catalog.all_services() if not s.requires_interview] == ["release_creation"]


def test_all_prices_are_payable_with_stripe(app):
    for s in catalog.all_services():
        assert all(catalog.MIN_PAYMENT_YEN <= p <= catalog.MAX_PAYMENT_YEN for p in s.all_prices()), s.key


def test_service_notes(app):
    assert catalog.SERVICE_NOTES == (
        "リリース作成以外のサービスは、面談が必須となります。",
        "複数のサービスを申し込む場合は、ご相談ください。",
        "その他、ご希望のサービスがあれば、ご相談ください。",
    )


def test_unknown_service_is_none(app):
    assert catalog.get_service("nope") is None


def test_refund_policy_text_covers_every_rule(app):
    text = catalog.refund_policy_text()
    for expected in ("全額を返金します", "50%を返金します", "「納品済み」：返金できません",
                     "当日、取材するメディアが一件もなければ、全額返金します",
                     "すでに書類の提出に使った分は、返金できません", "お問い合わせ先までご連絡ください"):
        assert expected in text, expected


def test_media_zero_rule_is_only_for_interview_planning(app):
    policy = dict(catalog.REFUND_POLICY)
    heading = next(h for h in policy if "メディアが一件もなければ" in h)
    assert heading.startswith("記者発表会の企画・当日の同席は")  # ほかのサービスには適用されない


# ---- 注文のモデル ----
def test_order_defaults(app, client_user):
    o = _order(client_user)
    assert o.status == "unpaid" and o.progress is None and o.refunded_amount == 0
    assert o.free_revisions_used == 0 and o.paid_at is None


def test_refund_cannot_exceed_amount(app, client_user):
    o = _order(client_user, amount=1000)
    o.refunded_amount = 1001
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_negative_amount_rejected(app, client_user):
    db.session.add(Order(user_id=client_user.id, service_type="x", service_name="x", amount_yen=-1))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_stripe_ids_are_unique(app, client_user):
    _order(client_user, stripe_session_id="cs_1", stripe_payment_intent_id="pi_1")
    db.session.add(Order(user_id=client_user.id, service_type="x", service_name="x", amount_yen=100,
                         stripe_session_id="cs_1"))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_several_orders_without_stripe_ids_are_allowed(app, client_user):
    _order(client_user)
    _order(client_user)  # 空（NULL）は重複してよい
    assert Order.query.count() == 2


def test_stripe_event_id_is_unique(app):
    db.session.add(StripeEvent(event_id="evt_1", event_type="x"))
    db.session.commit()
    db.session.add(StripeEvent(event_id="evt_1", event_type="x"))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_deleting_user_keeps_orders_with_null_user(app):
    u = make_user(email="gone@example.com")
    o = _order(u, status="paid", amount=60000, service_type="interview_planning")
    order_id = o.id
    db.session.delete(u)
    db.session.commit()
    kept = db.session.get(Order, order_id)
    assert kept is not None and kept.user_id is None and kept.amount_yen == 60000


# ---- 書類の枠（無料の枠＋購入した枠） ----
def test_slots_free_only(app, client_user):
    assert service.total_slots(client_user.id) == 1 and service.can_create(client_user)


def _first_submitted(user):
    r = Release(user_id=user.id, first_submitted_at=utcnow(), **FULL)
    db.session.add(r)
    db.session.commit()
    return r


def test_purchased_slot_allows_another_release(app, client_user):
    _first_submitted(client_user)
    assert not service.can_create(client_user)
    _order(client_user, status="paid")
    assert service.total_slots(client_user.id) == 2 and service.can_create(client_user)


def test_unpaid_canceled_and_refunded_orders_do_not_add_slots(app, client_user):
    _first_submitted(client_user)
    for status in ("unpaid", "canceled", "refunded"):
        _order(client_user, status=status)
    assert service.total_slots(client_user.id) == 1 and not service.can_create(client_user)


def test_other_services_do_not_add_slots(app, client_user):
    _first_submitted(client_user)
    _order(client_user, service_type="interview_planning", status="paid", amount=60000)
    assert not service.can_create(client_user)


def test_other_users_orders_do_not_add_slots(app, client_user):
    other = make_user(email="other@example.com")
    _first_submitted(client_user)
    _order(other, status="paid")
    assert not service.can_create(client_user)


def test_second_first_submission_needs_purchased_slot(app, client_user):
    _first_submitted(client_user)
    draft = Release(user_id=client_user.id, **FULL)
    db.session.add(draft)
    db.session.commit()
    with pytest.raises(service.ReleaseError, match="枠を使い切りました"):
        service.submit(draft)
    _order(client_user, status="paid")
    service.submit(draft)
    db.session.refresh(draft)
    assert draft.status == "pending"
    assert service.count_first_submitted(client_user.id) == 2
