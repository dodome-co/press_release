import pytest
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Release, ReleasePhoto, ReleaseScheduleItem, User

from .conftest import make_user


def _release(user, **kw):
    r = Release(user_id=user.id, title="新サービス発表", **kw)
    db.session.add(r)
    db.session.commit()
    return r


def test_release_defaults(app, client_user):
    r = _release(client_user)
    assert r.status == "draft"
    assert r.first_submitted_at is None
    assert r.summary is None and r.summary_source == "manual"
    assert r.features == []


def test_features_skip_empty(app, client_user):
    r = _release(client_user, feature_1="速い", feature_2="", feature_3="安い")
    assert r.features == ["速い", "安い"]


def test_photo_position_only_1_or_2(app, client_user):
    r = _release(client_user)
    db.session.add(ReleasePhoto(release_id=r.id, position=3, file_path="a", original_filename="a.png",
                                content_type="image/png", size_bytes=1))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_photo_position_unique_per_release(app, client_user):
    r = _release(client_user)
    for _ in range(2):
        db.session.add(ReleasePhoto(release_id=r.id, position=1, file_path="a", original_filename="a.png",
                                    content_type="image/png", size_bytes=1))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_schedule_position_range(app, client_user):
    r = _release(client_user)
    db.session.add(ReleaseScheduleItem(release_id=r.id, position=6, time_text="10:00", content="x"))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_deleting_release_deletes_children(app, client_user):
    r = _release(client_user)
    db.session.add(ReleasePhoto(release_id=r.id, position=1, file_path="a", original_filename="a.png",
                                content_type="image/png", size_bytes=1))
    db.session.add(ReleaseScheduleItem(release_id=r.id, position=1, time_text="10:00", content="x"))
    db.session.commit()
    db.session.delete(r)
    db.session.commit()
    assert ReleasePhoto.query.count() == 0 and ReleaseScheduleItem.query.count() == 0


def test_deleting_user_deletes_releases(app):
    u = make_user(email="gone@example.com")
    _release(u)
    db.session.delete(u)
    db.session.commit()
    assert Release.query.count() == 0
    assert User.query.count() == 0
