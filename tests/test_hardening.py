"""段階2のセキュリティ点検（競合・制御文字・メール失敗・画像の負荷など）への対策のテスト。"""
import io
import threading
import time
from unittest.mock import patch

import pytest
from docx import Document
from PIL import Image
from sqlalchemy.exc import IntegrityError
from werkzeug.datastructures import FileStorage

from app import create_app, docgen
from app.extensions import db
from app.models import Release, User, utcnow
from app.releases import service
from app.textutil import clean_text
from app.uploads import UploadError, resolve_path, save_image

from .conftest import login, make_user, png_bytes
from .test_admin_releases import _edit_data, _pending_release
from .test_releases import FULL, _new_release, _save


# ---- 競合：ファイルの SQLite で、本当に別々のスレッドから同時に操作する ----
@pytest.fixture()
def file_app(tmp_path, monkeypatch):
    from app.config import TestingConfig

    monkeypatch.setattr(TestingConfig, "SQLALCHEMY_DATABASE_URI", f"sqlite:///{tmp_path}/race.db")
    app = create_app("testing")
    app.config["UPLOAD_DIR"] = str(tmp_path / "race_uploads")
    with app.app_context():
        db.create_all()
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()


def _run_in_threads(app, jobs):
    """jobs: 引数なしの関数のリスト。各スレッドで app_context を作って同時に走らせ、結果（または例外）を返す。"""
    results = [None] * len(jobs)
    start = threading.Barrier(len(jobs))

    def runner(i, job):
        with app.app_context():
            start.wait()
            try:
                results[i] = ("ok", job())
            except Exception as e:  # noqa: BLE001
                results[i] = ("error", e)
            finally:
                db.session.remove()

    threads = [threading.Thread(target=runner, args=(i, j)) for i, j in enumerate(jobs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return results


def _slow_count(original):
    def wrapper(user_id):
        n = original(user_id)
        time.sleep(0.4)  # 「数えてから更新するまで」の隙間を、わざと広げる
        return n
    return wrapper


def test_two_simultaneous_first_submissions_respect_free_limit(file_app):
    with file_app.app_context():
        user = make_user(email="race@example.com")
        ids = []
        for _ in range(2):
            r = Release(user_id=user.id, **FULL)
            db.session.add(r)
            db.session.commit()
            ids.append(r.id)
        user_id = user.id

    def submit(release_id):
        def job():
            release = db.session.get(Release, release_id)
            service.submit(release)
        return job

    original = service.count_first_submitted
    with patch.object(service, "count_first_submitted", _slow_count(original)):
        results = _run_in_threads(file_app, [submit(ids[0]), submit(ids[1])])

    with file_app.app_context():
        statuses = sorted(db.session.get(Release, i).status for i in ids)
        assert statuses == ["draft", "pending"], (statuses, results)  # 1件だけ通る
        assert service.count_first_submitted(user_id) == 1
    errors = [r for r in results if r[0] == "error"]
    assert len(errors) == 1 and isinstance(errors[0][1], service.ReleaseError)


def test_two_simultaneous_approvals_only_one_wins(file_app):
    with file_app.app_context():
        user = make_user(email="race2@example.com")
        admin = make_user(email="admin2@example.com", role="admin")
        release = _pending_release(user)
        release.summary = "要旨"
        db.session.commit()
        release_id, admin_id = release.id, admin.id

    def approve():
        release = db.session.get(Release, release_id)
        service.approve(release, db.session.get(User, admin_id))

    results = _run_in_threads(file_app, [approve, approve])
    assert sorted(r[0] for r in results) == ["error", "ok"], results
    with file_app.app_context():
        assert db.session.get(Release, release_id).status == "approved"


def test_approve_and_reject_at_the_same_time_only_one_wins(file_app):
    with file_app.app_context():
        user = make_user(email="race3@example.com")
        admin = make_user(email="admin3@example.com", role="admin")
        release = _pending_release(user)
        release.summary = "要旨"
        db.session.commit()
        release_id, admin_id = release.id, admin.id

    def approve():
        service.approve(db.session.get(Release, release_id), db.session.get(User, admin_id))

    def reject():
        service.reject(db.session.get(Release, release_id), db.session.get(User, admin_id), "理由")

    results = _run_in_threads(file_app, [approve, reject])
    assert sorted(r[0] for r in results) == ["error", "ok"], results
    with file_app.app_context():
        release = db.session.get(Release, release_id)
        assert release.status in ("approved", "rejected")
        if release.status == "approved":
            assert release.file_path and not release.reject_reason
        else:
            assert release.file_path is None


def test_edit_after_submission_is_refused(app, client, client_user):
    """画面を開いたあとに提出された（承認待ちになった）書類は、保存できない。"""
    login(client, "client@example.com")
    release = _new_release(client)
    _save(client, release)
    stale_copy = db.session.get(Release, release.id)
    client.post(f"/releases/{release.id}/submit")  # 別のタブで提出
    with pytest.raises(service.ReleaseError):
        service.update_fields(stale_copy, {"title": "提出後に書き換え"})
    db.session.refresh(release)
    assert release.title == FULL["title"]


# ---- 制御文字 ----
def test_clean_text():
    assert clean_text("a\x01b\x00c\x0bd\x7fe") == "abcde"
    assert clean_text("  a\r\nb\rc\td  ") == "a\nb\nc\td"
    assert clean_text(None) == ""


def test_control_characters_removed_when_saving(client, client_user):
    login(client, "client@example.com")
    release = _new_release(client)
    _save(client, release, title="タイトル\x01\x02", service_description="概要\x00です")
    db.session.refresh(release)
    assert release.title == "タイトル" and release.service_description == "概要です"


def test_control_characters_do_not_break_word_ampersands(app, client_user):
    """制御文字があると、書類全体の & < > が消えてしまっていた問題。直接 DB に入れた場合でも守る。"""
    client_user.company_name = "A&B <C> \x01社"
    r = Release(user_id=client_user.id, summary="x\x02", approved_at=utcnow(), **{**FULL, "title": "t\x01 & <u>"})
    db.session.add(r)
    db.session.commit()
    doc = Document(io.BytesIO(docgen.render_release(r)))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "t & <u>" in text and "A&B <C> 社" in text


def test_control_characters_removed_from_profile_and_register(client, client_user):
    login(client, "client@example.com")
    data = {"company_name": "社\x01名", "postal_code": "100-0001", "address": "住\x02所", "contact_name": "担\x03当",
            "phone": "03-0000-0000"}
    client.post("/mypage/profile", data=data)
    u = db.session.get(User, client_user.id)
    assert (u.company_name, u.address, u.contact_name) == ("社名", "住所", "担当")


def test_admin_edit_removes_control_characters(app, client_user, admin_user):
    release = _pending_release(client_user)
    c = app.test_client()
    login(c, "admin@example.com")
    c.post(f"/admin/releases/{release.id}", data=_edit_data(summary="要旨\x01です"))
    db.session.refresh(release)
    assert release.summary == "要旨です"


# ---- メールの失敗で、状態の変更を台なしにしない ----
@pytest.fixture()
def broken_mail(app):
    app.config["MAIL_BACKEND"] = "not-implemented"  # 本番用が未実装のときと同じ


def test_submit_succeeds_even_if_mail_fails(client, client_user, admin_user, broken_mail):
    login(client, "client@example.com")
    release = _new_release(client)
    _save(client, release)
    res = client.post(f"/releases/{release.id}/submit")
    assert res.status_code == 302
    db.session.refresh(release)
    assert release.status == "pending"


def test_approve_and_reject_succeed_even_if_mail_fails(app, client_user, admin_user, broken_mail):
    a = _pending_release(client_user)
    b = _pending_release(client_user)
    c = app.test_client()
    login(c, "admin@example.com")
    c.post(f"/admin/releases/{a.id}", data=_edit_data())
    assert c.post(f"/admin/releases/{a.id}/approve").status_code == 302
    assert c.post(f"/admin/releases/{b.id}/reject", data={"reason": "理由"}).status_code == 302
    db.session.refresh(a)
    db.session.refresh(b)
    assert (a.status, b.status) == ("approved", "rejected")


def test_password_reset_same_response_even_if_mail_fails(app, client, client_user, broken_mail):
    known = client.post("/password/reset", data={"email": "client@example.com"})
    unknown = client.post("/password/reset", data={"email": "nobody@example.com"})
    assert known.status_code == unknown.status_code == 200
    assert known.get_data() == unknown.get_data()


# ---- 画像 ----
def test_default_pixel_limit_is_smaller(app):
    assert app.config["UPLOAD_MAX_PIXELS"] == 16_000_000


def test_image_over_pixel_limit_rejected_with_default(app):
    big = io.BytesIO()
    Image.new("L", (4100, 4100), 255).save(big, "PNG")  # 約1,680万画素・小さなファイル
    assert len(big.getvalue()) < app.config["UPLOAD_MAX_BYTES"]
    with pytest.raises(UploadError):
        save_image(FileStorage(stream=io.BytesIO(big.getvalue()), filename="big.png"), "x")


def test_jpeg_comment_is_removed(app):
    buf = io.BytesIO()
    Image.new("RGB", (30, 30), (1, 2, 3)).save(buf, "JPEG", comment=b"SECRET-COMMENT")
    assert b"SECRET-COMMENT" in buf.getvalue()
    saved = save_image(FileStorage(stream=io.BytesIO(buf.getvalue()), filename="a.jpg"), "x")
    assert b"SECRET-COMMENT" not in resolve_path(saved.relative_path).read_bytes()


def test_photo_conflict_becomes_message_not_500(app, client, client_user):
    login(client, "client@example.com")
    release = _new_release(client)
    with patch.object(db.session, "flush", side_effect=IntegrityError("x", {}, Exception("dup"))):
        res = client.post(f"/releases/{release.id}/photos",
                          data={"photo": (io.BytesIO(png_bytes()), "a.png")},
                          content_type="multipart/form-data", follow_redirects=True)
    assert res.status_code == 200
    assert "もう一度お試しください" in res.get_data(as_text=True)
    db.session.refresh(release)
    assert release.photos == []
    # 保存途中のファイルが残っていない
    folder = resolve_path(f"releases/{release.id}/photos")
    assert not folder.exists() or list(folder.iterdir()) == []


# ---- 下書きの上限・理由の長さ・管理者の下書き写真 ----
def test_draft_limit(client, client_user, app):
    app.config["DRAFT_LIMIT"] = 2
    login(client, "client@example.com")
    client.post("/releases/new")
    client.post("/releases/new")
    res = client.post("/releases/new", follow_redirects=True)
    assert "下書きは2件まで" in res.get_data(as_text=True)
    assert Release.query.count() == 2
    # 削除すれば、また作れる
    first = Release.query.first()
    client.post(f"/releases/{first.id}/delete")
    client.post("/releases/new")
    assert Release.query.count() == 2


def test_reject_reason_length_limit(app, client_user, admin_user):
    release = _pending_release(client_user)
    c = app.test_client()
    login(c, "admin@example.com")
    res = c.post(f"/admin/releases/{release.id}/reject", data={"reason": "あ" * 1001}, follow_redirects=True)
    assert "1000文字以内" in res.get_data(as_text=True)
    db.session.refresh(release)
    assert release.status == "pending"
    assert c.post(f"/admin/releases/{release.id}/reject", data={"reason": "あ" * 1000}).status_code == 302
    db.session.refresh(release)
    assert release.status == "rejected"
