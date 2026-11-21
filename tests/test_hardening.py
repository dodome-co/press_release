"""段階2のセキュリティ点検（競合・制御文字・メール失敗・画像の負荷など）への対策のテスト。"""
import io
import threading
import time
from unittest.mock import patch

import pytest
from PIL import Image
from sqlalchemy.exc import IntegrityError
from werkzeug.datastructures import FileStorage

from app import create_app, pdfgen
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
        service.update_fields(stale_copy, {"location": "提出後に書き換え"})
    db.session.refresh(release)
    assert release.location == FULL["location"]


# ---- 制御文字 ----
def test_clean_text():
    assert clean_text("a\x01b\x00c\x0bd\x7fe") == "abcde"
    assert clean_text("  a\r\nb\rc\td  ") == "a\nb\nc\td"
    assert clean_text(None) == ""


def test_control_characters_removed_when_saving(client, client_user):
    login(client, "client@example.com")
    release = _new_release(client)
    _save(client, release)
    client.post(f"/releases/{release.id}/edit", data={"location": "会場\x01\x02です"})
    db.session.refresh(release)
    assert release.location == "会場です"


def test_control_characters_do_not_break_pdf_ampersands(app, client_user):
    """制御文字があっても、書類全体の & < > が壊れない。直接 DB に入れた場合でも守る。"""
    import re

    from pypdf import PdfReader

    client_user.company_name = "A&B <C> \x01社"
    r = Release(user_id=client_user.id, summary="x\x02", approved_at=utcnow(), **{**FULL, "title": "t\x01 & <u>"})
    db.session.add(r)
    db.session.commit()
    text = re.sub(r"\s+", "", "".join(p.extract_text() for p in PdfReader(io.BytesIO(pdfgen.render_release(r).data)).pages))
    assert "t&<u>" in text and "A&B<C>社" in text


def test_control_characters_removed_from_profile_and_register(client, client_user):
    login(client, "client@example.com")
    data = {"company_name": "社\x01名", "postal_code": "100-0001", "address": "東京都\x02千代田区", "contact_name": "担\x03当",
            "phone": "03-0000-0000"}
    client.post("/mypage/profile", data=data)
    u = db.session.get(User, client_user.id)
    assert (u.company_name, u.address, u.contact_name) == ("社名", "東京都千代田区", "担当")


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


# ---- タイトルの長さ（全角22文字以内） ----
def test_display_width():
    from app.textutil import display_width

    assert display_width("あいう") == 3
    assert display_width("ABC") == 1.5  # 半角は0.5
    assert display_width("Ａ") == 1  # 全角英字は1
    assert display_width("あA") == 1.5
    assert display_width(None) == 0


@pytest.fixture()
def admin_edit(app, client_user, admin_user):
    """管理者の画面で、承認待ちの書類のタイトルを保存する（タイトルは、管理者が決める）。"""
    release = _pending_release(client_user)
    c = app.test_client()
    login(c, "admin@example.com")

    def edit(title):
        return c.post(f"/admin/releases/{release.id}", data=_edit_data(title=title))

    edit.release = release
    return edit


def test_title_of_22_fullwidth_chars_is_accepted(admin_edit):
    title = "あ" * 22
    assert admin_edit(title).status_code == 302
    db.session.refresh(admin_edit.release)
    assert admin_edit.release.title == title


def test_title_of_23_fullwidth_chars_is_rejected(admin_edit):
    res = admin_edit("あ" * 23)
    assert res.status_code == 200
    assert "全角22文字以内" in res.get_data(as_text=True)
    db.session.refresh(admin_edit.release)
    assert admin_edit.release.title == FULL["title"]  # 変わらない


def test_halfwidth_chars_count_as_half(admin_edit):
    assert admin_edit("a" * 44).status_code == 302  # 半角44文字 = 全角22文字
    assert admin_edit("a" * 45).status_code == 200  # 超えると拒否


def test_mixed_title_boundary(admin_edit):
    assert admin_edit("あ" * 21 + "ab").status_code == 302  # 21 + 1 = 22
    assert admin_edit("あ" * 21 + "abc").status_code == 200  # 22.5


def test_service_also_guards_title(app, client_user, admin_user):
    release = _pending_release(client_user)
    with pytest.raises(service.ReleaseError, match="全角22文字"):
        service.admin_update(release, admin_user, {"title": "あ" * 23}, "要旨")


def test_client_cannot_set_the_title(client, client_user):
    """タイトルは、管理者が決める。クライアントのフォームでは、変えられない。"""
    login(client, "client@example.com")
    release = _new_release(client)
    client.post(f"/releases/{release.id}/edit", data={"location": "会場", "title": "勝手なタイトル"})
    db.session.refresh(release)
    assert release.title == ""


def test_title_limit_is_configurable(admin_edit, app):
    app.config["TITLE_MAX_WIDTH"] = 10
    assert admin_edit("あ" * 11).status_code == 200
    assert admin_edit("あ" * 10).status_code == 302


# ---- 写真は横長だけ（ロゴは対象外） ----
def _png(w, h):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 20, 30)).save(buf, "PNG")
    return buf.getvalue()


def _upload_photo(client, release_id, data, name="p.png", base="/releases"):
    return client.post(f"{base}/{release_id}/photos", data={"photo": (io.BytesIO(data), name)},
                       content_type="multipart/form-data", follow_redirects=True)


def test_landscape_photo_accepted(client, client_user):
    login(client, "client@example.com")
    release = _new_release(client)
    _upload_photo(client, release.id, _png(300, 200))  # 1.5
    db.session.refresh(release)
    assert len(release.photos) == 1


def test_portrait_photo_rejected(client, client_user):
    login(client, "client@example.com")
    release = _new_release(client)
    res = _upload_photo(client, release.id, _png(200, 300))
    assert "横長の写真" in res.get_data(as_text=True)
    db.session.refresh(release)
    assert release.photos == []


def test_square_and_nearly_square_photos_rejected(client, client_user):
    login(client, "client@example.com")
    release = _new_release(client)
    for size in ((200, 200), (210, 200)):  # 1.0 と 1.05
        _upload_photo(client, release.id, _png(*size))
    db.session.refresh(release)
    assert release.photos == []


def test_aspect_boundary_is_inclusive(client, client_user):
    login(client, "client@example.com")
    release = _new_release(client)
    _upload_photo(client, release.id, _png(240, 200))  # ちょうど 1.2
    db.session.refresh(release)
    assert len(release.photos) == 1


def test_orientation_is_judged_after_exif_rotation(client, client_user):
    """横に保存されていても、EXIF で縦向きとされている写真は、見た目どおり縦長として拒否する。"""
    exif = Image.Exif()
    exif[0x0112] = 6  # 90度回転して表示する
    buf = io.BytesIO()
    Image.new("RGB", (300, 200), (1, 2, 3)).save(buf, "JPEG", exif=exif.tobytes())
    login(client, "client@example.com")
    release = _new_release(client)
    res = _upload_photo(client, release.id, buf.getvalue(), name="a.jpg")
    assert "横長の写真" in res.get_data(as_text=True)


def test_saved_landscape_photo_stays_landscape(client, client_user):
    login(client, "client@example.com")
    release = _new_release(client)
    _upload_photo(client, release.id, _png(300, 200))
    db.session.refresh(release)
    with Image.open(resolve_path(release.photos[0].file_path)) as img:
        assert img.size == (300, 200)


def test_admin_photo_must_be_landscape_too(app, client_user, admin_user):
    release = _pending_release(client_user)
    c = app.test_client()
    login(c, "admin@example.com")
    res = _upload_photo(c, release.id, _png(200, 300), base="/admin/releases")
    assert "横長の写真" in res.get_data(as_text=True)
    db.session.refresh(release)
    assert release.photos == []
    _upload_photo(c, release.id, _png(300, 200), base="/admin/releases")
    db.session.refresh(release)
    assert len(release.photos) == 1


def test_logo_can_be_portrait_or_square(client, client_user):
    login(client, "client@example.com")
    for size in ((200, 300), (200, 200)):
        client.post("/mypage/profile/logo", data={"logo": (io.BytesIO(_png(*size)), "l.png")},
                    content_type="multipart/form-data")
        assert db.session.get(User, client_user.id).logo_path


def test_two_landscape_photos_are_in_the_pdf(app, client, client_user):
    from pypdf import PdfReader

    login(client, "client@example.com")
    release = _new_release(client)
    _upload_photo(client, release.id, _png(300, 200))
    _upload_photo(client, release.id, _png(320, 180))
    db.session.refresh(release)
    release.summary, release.approved_at = "要旨", utcnow()
    db.session.commit()
    pdf = PdfReader(io.BytesIO(pdfgen.render_release(release).data))
    assert sum(len(page.images) for page in pdf.pages) == 2

