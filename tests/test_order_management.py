import io
import zipfile
from datetime import date, timedelta

import pytest
from docx import Document

from app.extensions import db
from app.models import Order, User, utcnow
from app.orders import management
from app.uploads import UploadError, resolve_path, save_document

from .conftest import login, make_user
from .test_orders import _mails


def _order(user, service_type="interview_planning", status="paid", progress="received", amount=60000, **kw):
    kw.setdefault("paid_at", utcnow())
    o = Order(user_id=user.id if user else None, service_type=service_type, service_name="テスト",
              amount_yen=amount, status=status, progress=progress, **kw)
    db.session.add(o)
    db.session.commit()
    return o


def _docx_bytes(text="記事の本文"):
    buf = io.BytesIO()
    Document().save(buf)
    doc = Document()
    doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _post_file(client, order_id, data, name, base="/admin/orders"):
    return client.post(f"{base}/{order_id}/deliver-article", data={"file": (io.BytesIO(data), name)},
                       content_type="multipart/form-data", follow_redirects=True)


@pytest.fixture()
def admin_client(app, admin_user):
    c = app.test_client()
    login(c, "admin@example.com")
    return c


@pytest.fixture()
def client_logged_in(client, client_user):
    login(client, "client@example.com")
    return client


# ================= マイページ（F14） =================
def test_mypage_requires_login(client):
    assert client.get("/mypage/orders").status_code == 302


def test_admin_is_redirected_from_mypage(admin_client):
    res = admin_client.get("/mypage/")
    assert res.status_code == 302 and "/admin/" in res.headers["Location"]
    assert admin_client.get("/mypage/orders").status_code == 403


def test_mypage_lists_only_own_orders(app, client_logged_in, client_user):
    mine = _order(client_user, service_type="distribution_proxy")
    other = make_user(email="other@example.com")
    theirs = _order(other, service_type="interview_accompany")
    body = client_logged_in.get("/mypage/orders").get_data(as_text=True)
    assert f"/mypage/orders/{mine.id}" in body and f"/mypage/orders/{theirs.id}" not in body
    assert "お支払い済み" in body and "受付" in body


def test_mypage_dashboard_shows_recent_orders(client_logged_in, client_user):
    _order(client_user)
    body = client_logged_in.get("/mypage/").get_data(as_text=True)
    assert "最近の注文" in body and "テスト" in body


def test_order_detail_shows_progress_due_and_refund(client_logged_in, client_user):
    o = _order(client_user, progress="in_progress", due_date=date(2026, 12, 25), refunded_amount=30000,
               status="partially_refunded")
    body = client_logged_in.get(f"/mypage/orders/{o.id}").get_data(as_text=True)
    for expected in ("着手", "2026-12-25", "一部返金", "30,000円"):
        assert expected in body, expected


def test_other_users_order_detail_is_404(app, client_logged_in):
    other = make_user(email="other@example.com")
    o = _order(other)
    assert client_logged_in.get(f"/mypage/orders/{o.id}").status_code == 404
    assert client_logged_in.get(f"/mypage/orders/{o.id}/delivery").status_code == 404


def test_unpaid_order_shows_notice(client_logged_in, client_user):
    o = _order(client_user, status="unpaid", progress=None, paid_at=None)
    assert "お支払いが確認できていません" in client_logged_in.get(f"/mypage/orders/{o.id}").get_data(as_text=True)


def test_materials_url_is_text_not_a_link(client_logged_in, client_user):
    o = _order(client_user, service_type="video_creation", materials_url="https://drive.example.com/x")
    body = client_logged_in.get(f"/mypage/orders/{o.id}").get_data(as_text=True)
    assert "https://drive.example.com/x" in body and 'href="https://drive.example.com' not in body


# ================= 管理者の注文一覧・詳細（F16） =================
def test_admin_order_pages_need_admin(client, client_user):
    o = _order(client_user)
    assert client.get("/admin/orders").status_code == 302
    login(client, "client@example.com")
    for path in ("", f"/{o.id}"):
        assert client.get(f"/admin/orders{path}").status_code == 403
    for action in ("due-date", "start", "revisions", "deliver-video", "mark-delivered"):
        assert client.post(f"/admin/orders/{o.id}/{action}").status_code == 403


def test_admin_list_filters(app, admin_client, client_user):
    a = _order(client_user, service_type="video_creation", progress="in_progress")
    b = _order(client_user, service_type="interview_planning", progress="received")
    c = _order(client_user, service_type="article_creation", status="canceled", progress=None)
    body = admin_client.get("/admin/orders?status=paid").get_data(as_text=True)
    assert f"/admin/orders/{a.id}" in body and f"/admin/orders/{c.id}" not in body
    body = admin_client.get("/admin/orders?progress=in_progress").get_data(as_text=True)
    assert f"/admin/orders/{a.id}" in body and f"/admin/orders/{b.id}" not in body
    body = admin_client.get("/admin/orders?service=interview_planning").get_data(as_text=True)
    assert f"/admin/orders/{b.id}" in body and f"/admin/orders/{a.id}" not in body
    assert admin_client.get("/admin/orders?status=<script>").status_code == 200  # 不正な値は無視される


def test_admin_detail_shows_client_and_materials(admin_client, client_user):
    o = _order(client_user, service_type="video_creation", materials_url="https://drive.example.com/x",
               stripe_payment_intent_id="pi_123")
    body = admin_client.get(f"/admin/orders/{o.id}").get_data(as_text=True)
    for expected in ("株式会社テスト", "client@example.com", "https://drive.example.com/x", "pi_123"):
        assert expected in body, expected


def test_admin_list_handles_withdrawn_user(admin_client):
    _order(None)
    assert "退会済み" in admin_client.get("/admin/orders").get_data(as_text=True)


# ================= 納期・進捗・修正回数（F19） =================
def test_set_and_clear_due_date(admin_client, client_user):
    o = _order(client_user)
    due = (date.today() + timedelta(days=14)).isoformat()
    admin_client.post(f"/admin/orders/{o.id}/due-date", data={"due_date": due})
    db.session.refresh(o)
    assert o.due_date.isoformat() == due
    admin_client.post(f"/admin/orders/{o.id}/due-date", data={"due_date": ""})
    db.session.refresh(o)
    assert o.due_date is None


@pytest.mark.parametrize("bad", ["abc", "2026-13-40", "2020-01-01", "2999-01-01"])
def test_bad_due_dates_are_rejected(admin_client, client_user, bad):
    o = _order(client_user)
    admin_client.post(f"/admin/orders/{o.id}/due-date", data={"due_date": bad})
    db.session.refresh(o)
    assert o.due_date is None


def test_start_work_only_from_received_and_never_back(admin_client, client_user):
    o = _order(client_user)
    admin_client.post(f"/admin/orders/{o.id}/start")
    db.session.refresh(o)
    assert o.progress == "in_progress"
    res = admin_client.post(f"/admin/orders/{o.id}/start", follow_redirects=True)
    assert "「受付」の注文だけ" in res.get_data(as_text=True)
    db.session.refresh(o)
    assert o.progress == "in_progress"


def test_revision_count(admin_client, client_user):
    o = _order(client_user)
    admin_client.post(f"/admin/orders/{o.id}/revisions", data={"free_revisions_used": "1"})
    db.session.refresh(o)
    assert o.free_revisions_used == 1
    for bad in ("-1", "100", "x", ""):
        admin_client.post(f"/admin/orders/{o.id}/revisions", data={"free_revisions_used": bad})
    db.session.refresh(o)
    assert o.free_revisions_used == 1


@pytest.mark.parametrize("status", ["unpaid", "canceled", "refunded"])
def test_no_work_on_orders_that_are_not_paid(admin_client, client_user, status):
    o = _order(client_user, status=status, progress=None)
    for action, data in (("start", {}), ("revisions", {"free_revisions_used": "1"}),
                         ("due-date", {"due_date": date.today().isoformat()}), ("mark-delivered", {})):
        admin_client.post(f"/admin/orders/{o.id}/{action}", data=data)
    db.session.refresh(o)
    assert o.progress is None and o.due_date is None and o.free_revisions_used == 0


def test_partially_refunded_order_can_still_be_worked_on(admin_client, client_user):
    o = _order(client_user, status="partially_refunded", progress="in_progress", refunded_amount=30000)
    admin_client.post(f"/admin/orders/{o.id}/mark-delivered")
    db.session.refresh(o)
    assert o.progress == "delivered"


def test_release_creation_order_has_no_delivery_or_progress(admin_client, client_user):
    o = _order(client_user, service_type="release_creation", progress=None, amount=5000)
    for action in ("start", "mark-delivered", "revisions"):
        admin_client.post(f"/admin/orders/{o.id}/{action}", data={"free_revisions_used": "1"})
    db.session.refresh(o)
    assert o.progress is None and o.delivered_at is None
    assert "納期・進捗・納品はありません" in admin_client.get(f"/admin/orders/{o.id}").get_data(as_text=True)


# ================= 納品（F22） =================
def test_deliver_docx_pdf_and_txt(app, admin_client, client_logged_in, client_user):
    for name, data in (("記事.docx", _docx_bytes()), ("記事.pdf", b"%PDF-1.7\n..."), ("記事.txt", "本文です".encode())):
        o = _order(client_user, service_type="article_creation")
        res = _post_file(admin_client, o.id, data, name)
        assert "納品しました" in res.get_data(as_text=True), name
        db.session.refresh(o)
        assert o.progress == "delivered" and o.delivered_at is not None and o.delivery_filename == name
        assert resolve_path(o.delivery_file_path).is_file()
        assert o.delivery_file_path.startswith(f"orders/{o.id}/delivery/")


def test_client_can_download_delivery_as_attachment(admin_client, client_logged_in, client_user):
    o = _order(client_user, service_type="article_creation")
    _post_file(admin_client, o.id, _docx_bytes("納品の本文"), "納品.docx")
    res = client_logged_in.get(f"/mypage/orders/{o.id}/delivery")
    assert res.status_code == 200
    assert "attachment" in res.headers["Content-Disposition"]
    assert res.mimetype == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    assert "no-store" in res.headers["Cache-Control"]
    assert "納品の本文" in "\n".join(p.text for p in Document(io.BytesIO(res.data)).paragraphs)


def test_txt_is_served_as_download_not_inline_html(admin_client, client_logged_in, client_user):
    o = _order(client_user, service_type="article_creation")
    _post_file(admin_client, o.id, b"<script>alert(1)</script>", "x.txt")
    res = client_logged_in.get(f"/mypage/orders/{o.id}/delivery")
    assert res.mimetype == "text/plain" and "attachment" in res.headers["Content-Disposition"]
    assert res.headers["X-Content-Type-Options"] == "nosniff"


def test_other_client_cannot_download_delivery(app, admin_client, client_user):
    o = _order(client_user, service_type="article_creation")
    _post_file(admin_client, o.id, _docx_bytes(), "a.docx")
    other = app.test_client()
    make_user(email="other@example.com")
    login(other, "other@example.com")
    assert other.get(f"/mypage/orders/{o.id}/delivery").status_code == 404
    assert app.test_client().get(f"/mypage/orders/{o.id}/delivery").status_code == 302


def test_admin_can_download_delivery(admin_client, client_user):
    o = _order(client_user, service_type="article_creation")
    _post_file(admin_client, o.id, _docx_bytes(), "a.docx")
    assert admin_client.get(f"/admin/orders/{o.id}/delivery").status_code == 200


def test_redelivery_replaces_file_and_deletes_old_one(admin_client, client_user):
    o = _order(client_user, service_type="article_creation")
    _post_file(admin_client, o.id, _docx_bytes("初版"), "v1.docx")
    db.session.refresh(o)
    old = resolve_path(o.delivery_file_path)
    first_delivered = o.delivered_at
    _post_file(admin_client, o.id, _docx_bytes("修正版"), "v2.docx")
    db.session.refresh(o)
    assert not old.exists() and resolve_path(o.delivery_file_path).exists()
    assert o.delivery_filename == "v2.docx" and o.delivered_at == first_delivered  # 最初の納品日時は変えない


@pytest.mark.parametrize("name,data,message", [
    ("evil.exe", b"MZ\x90\x00", "PDF"),
    ("fake.pdf", b"not a pdf", "PDF として読み込めません"),
    ("fake.docx", b"not a zip", "Word"),
    ("plain.docx", b"PK\x05\x06" + b"\x00" * 18, "Word"),
    ("bin.txt", b"abc\x00def", "テキスト"),
    ("sjis.txt", "あいう".encode("cp932"), "UTF-8"),
    ("empty.txt", b"", "選択されていません"),
    ("noext", b"abc", "PDF"),
    ("page.html", b"<html></html>", "PDF"),
    ("image.png", b"\x89PNG", "PDF"),
])
def test_bad_article_files_are_rejected(admin_client, client_user, name, data, message):
    o = _order(client_user, service_type="article_creation")
    res = _post_file(admin_client, o.id, data, name)
    assert message in res.get_data(as_text=True), name
    db.session.refresh(o)
    assert o.delivery_file_path is None and o.progress == "received"


def test_zip_without_word_parts_is_not_docx(admin_client, client_user):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("hello.txt", "hi")
    o = _order(client_user, service_type="article_creation")
    assert "Word" in _post_file(admin_client, o.id, buf.getvalue(), "x.docx").get_data(as_text=True)


def test_oversize_article_is_rejected(app, admin_client, client_user):
    app.config["UPLOAD_MAX_DOCUMENT_BYTES"] = 100
    o = _order(client_user, service_type="article_creation")
    res = _post_file(admin_client, o.id, b"a" * 200, "x.txt")
    assert "以内にしてください" in res.get_data(as_text=True)


def test_filename_with_control_characters_is_cleaned(app):
    from werkzeug.datastructures import FileStorage

    saved = save_document(FileStorage(stream=io.BytesIO(b"abc"), filename="a\r\nb../../x.txt"), "x")
    assert "\n" not in saved.original_filename and "\r" not in saved.original_filename and "/" not in saved.original_filename
    assert ".." not in saved.relative_path


def test_save_document_rejects_unknown_extension(app):
    from werkzeug.datastructures import FileStorage

    with pytest.raises(UploadError):
        save_document(FileStorage(stream=io.BytesIO(b"abc"), filename="a.sh"), "x")


def test_article_cannot_be_delivered_to_other_services(admin_client, client_user):
    o = _order(client_user, service_type="video_creation")
    res = _post_file(admin_client, o.id, _docx_bytes(), "a.docx")
    assert "記事作成の注文だけ" in res.get_data(as_text=True)


def test_deliver_video(app, admin_client, client_logged_in, client_user):
    o = _order(client_user, service_type="video_creation")
    for url in ("https://www.youtube.com/watch?v=abc123", "https://youtu.be/abc123"):
        admin_client.post(f"/admin/orders/{o.id}/deliver-video", data={"delivery_url": url})
        db.session.refresh(o)
        assert o.delivery_url == url and o.progress == "delivered"
    body = client_logged_in.get(f"/mypage/orders/{o.id}").get_data(as_text=True)
    assert 'href="https://youtu.be/abc123"' in body and 'rel="noopener noreferrer"' in body


@pytest.mark.parametrize("url", ["http://www.youtube.com/watch?v=a", "https://evil.example.com/watch?v=a",
                                 "https://youtube.com.evil.example/x", "javascript:alert(1)", "https://notyoutube.com/x",
                                 "https://www.youtube.com/watch?v=a\nb", "", "https://user:pw@www.youtube.com/x"])
def test_bad_video_urls_are_rejected(admin_client, client_user, url):
    o = _order(client_user, service_type="video_creation")
    admin_client.post(f"/admin/orders/{o.id}/deliver-video", data={"delivery_url": url})
    db.session.refresh(o)
    assert o.delivery_url is None and o.progress == "received"


def test_video_cannot_be_delivered_to_other_services(admin_client, client_user):
    o = _order(client_user, service_type="article_creation")
    admin_client.post(f"/admin/orders/{o.id}/deliver-video", data={"delivery_url": "https://youtu.be/a"})
    db.session.refresh(o)
    assert o.delivery_url is None


def test_mark_delivered_for_services_without_files(admin_client, client_user):
    o = _order(client_user, service_type="interview_planning")
    admin_client.post(f"/admin/orders/{o.id}/mark-delivered")
    db.session.refresh(o)
    assert o.progress == "delivered" and o.delivered_at is not None
    res = admin_client.post(f"/admin/orders/{o.id}/mark-delivered", follow_redirects=True)
    assert "すでに納品済み" in res.get_data(as_text=True)


def test_article_and_video_cannot_be_marked_delivered_without_delivery(admin_client, client_user):
    for service_type in ("article_creation", "video_creation"):
        o = _order(client_user, service_type=service_type)
        admin_client.post(f"/admin/orders/{o.id}/mark-delivered")
        db.session.refresh(o)
        assert o.progress == "received"


def test_delivery_sends_email_to_client(app, admin_client, client_user):
    o = _order(client_user, service_type="video_creation")
    admin_client.post(f"/admin/orders/{o.id}/deliver-video", data={"delivery_url": "https://youtu.be/abc"})
    mail = next(m for m in _mails(app) if m["to"] == "client@example.com")
    assert "納品が完了しました" in mail["subject"] and f"/mypage/orders/{o.id}" in mail["body"]


def test_delivery_succeeds_even_if_mail_fails(app, admin_client, client_user):
    app.config["MAIL_BACKEND"] = "not-implemented"
    o = _order(client_user, service_type="video_creation")
    admin_client.post(f"/admin/orders/{o.id}/deliver-video", data={"delivery_url": "https://youtu.be/abc"})
    db.session.refresh(o)
    assert o.progress == "delivered"


def test_failed_delivery_sends_no_email(app, admin_client, client_user):
    o = _order(client_user, service_type="video_creation")
    admin_client.post(f"/admin/orders/{o.id}/deliver-video", data={"delivery_url": "http://bad"})
    assert not [m for m in _mails(app) if m["to"] == "client@example.com"]


def test_delivery_requires_workable_order(admin_client, client_user):
    o = _order(client_user, service_type="video_creation", status="unpaid", progress=None)
    admin_client.post(f"/admin/orders/{o.id}/deliver-video", data={"delivery_url": "https://youtu.be/a"})
    db.session.refresh(o)
    assert o.delivery_url is None


def test_management_error_does_not_leave_orphan_file(admin_client, client_user, monkeypatch):
    o = _order(client_user, service_type="article_creation")

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(db.session, "commit", lambda: boom())
    with pytest.raises(RuntimeError):
        management.deliver_article(o, type("F", (), {"filename": "a.txt", "stream": io.BytesIO(b"abc")})())
    folder = resolve_path(f"orders/{o.id}/delivery")
    assert not folder.exists() or list(folder.iterdir()) == []
