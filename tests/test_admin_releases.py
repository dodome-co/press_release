import io
import json
from unittest.mock import patch

import pytest
from pypdf import PdfReader

from app.extensions import db
from app.models import Release, ReleaseEdit, ReleasePhoto, ReleaseScheduleItem, utcnow
from app.releases import service
from app.uploads import resolve_path

from .conftest import login, make_user, png_bytes
from .test_releases import FORM_FIELDS, FULL, form_data


def _pdf_text(source) -> str:
    """PDF の文字（空白・改行を除く）。ファイルのパスか、PDF の中身（バイト列）を渡す。"""
    import re

    reader = PdfReader(str(source) if not isinstance(source, bytes) else io.BytesIO(source))
    return re.sub(r"\s+", "", "".join(page.extract_text() for page in reader.pages))


def _pending_release(user, **overrides):
    values = {**FULL, **overrides}
    r = Release(user_id=user.id, status="pending", first_submitted_at=utcnow(), submitted_at=utcnow(), **values)
    db.session.add(r)
    db.session.commit()
    return r


@pytest.fixture()
def admin_client(app, admin_user):
    c = app.test_client()
    login(c, "admin@example.com")
    return c


@pytest.fixture()
def pending(client_user):
    return _pending_release(client_user)


def _edit_data(**overrides):
    data = {**form_data({k: FULL[k] for k in ("title", "event_date", "event_start_time", "event_end_time", "location",
                                              "feature_1", "feature_2", "feature_3")}),
            "subtitle": "", "speakers": "", "features_heading": "", "summary": "管理者が書いた要旨です。"}
    data.update(overrides)
    return data


# ---- アクセス制御・一覧 ----
def test_admin_pages_require_admin(client, client_user, pending):
    assert client.get("/admin/releases").status_code == 302
    login(client, "client@example.com")
    assert client.get("/admin/releases").status_code == 403
    assert client.get(f"/admin/releases/{pending.id}").status_code == 403
    assert client.post(f"/admin/releases/{pending.id}/approve").status_code == 403
    assert client.post(f"/admin/releases/{pending.id}/reject", data={"reason": "x"}).status_code == 403


def test_drafts_are_hidden_from_admin(admin_client, client_user):
    draft = Release(user_id=client_user.id, title="下書き")
    db.session.add(draft)
    db.session.commit()
    assert "下書き" not in admin_client.get("/admin/releases").get_data(as_text=True)
    assert admin_client.get(f"/admin/releases/{draft.id}").status_code == 404


def test_list_filters_by_status_and_orders_pending_oldest_first(admin_client, client_user):
    from datetime import timedelta

    old = _pending_release(client_user, title="古い提出")
    new = _pending_release(client_user, title="新しい提出")
    old.submitted_at = utcnow() - timedelta(days=2)
    approved = _pending_release(client_user, title="承認済みの書類")
    approved.status = "approved"
    db.session.commit()
    body = admin_client.get("/admin/releases").get_data(as_text=True)
    assert body.index("古い提出") < body.index("新しい提出")
    pending_only = admin_client.get("/admin/releases?status=pending").get_data(as_text=True)
    assert "古い提出" in pending_only and "承認済みの書類" not in pending_only


# ---- 編集と履歴 ----
def test_admin_edit_records_only_changed_fields(admin_client, pending, admin_user):
    res = admin_client.post(f"/admin/releases/{pending.id}", data=_edit_data(title="直したタイトル"))
    assert res.status_code == 302
    db.session.refresh(pending)
    assert pending.title == "直したタイトル" and pending.summary == "管理者が書いた要旨です。"
    fields = sorted(e.field_name for e in pending.edits)
    assert fields == ["summary", "title"]  # 変えた項目だけ
    title_edit = next(e for e in pending.edits if e.field_name == "title")
    assert (title_edit.old_value, title_edit.new_value) == (FULL["title"], "直したタイトル")
    assert title_edit.edited_by == admin_user.id
    assert pending.last_edited_by == admin_user.id and pending.last_edited_at is not None


def test_admin_edit_without_changes_records_nothing(admin_client, pending):
    admin_client.post(f"/admin/releases/{pending.id}", data=_edit_data(summary=""))
    db.session.refresh(pending)
    assert pending.edits == [] and pending.last_edited_at is None


def test_admin_edit_subtitle_speakers_and_features_recorded(admin_client, pending):
    data = _edit_data(subtitle="地域の農家と連携", speakers="社長　岩手　盛子\n生産者　花巻　賢治", feature_2="とても安い")
    admin_client.post(f"/admin/releases/{pending.id}", data=data)
    db.session.refresh(pending)
    assert pending.subtitle == "地域の農家と連携" and pending.speaker_list == ["社長　岩手　盛子", "生産者　花巻　賢治"]
    assert pending.feature_2 == "とても安い"
    assert {e.field_name for e in pending.edits} == {"subtitle", "speakers", "feature_2", "summary"}


def test_admin_form_has_no_schedule_or_application(admin_client, pending):
    body = admin_client.get(f"/admin/releases/{pending.id}").get_data(as_text=True)
    for gone in ("schedule_time_1", "application_method", "service_description", "取材の申込方法"):
        assert gone not in body, gone
    for expected in ('name="subtitle"', 'name="speakers"', 'name="title"', 'name="features_heading"'):
        assert expected in body, expected
    assert "全角22文字" not in body and "title-candidates" not in body  # タイトルの字数の制限は、なくした


OPTIONS = [{"title": "案その1", "subtitle": "サブその1"}, {"title": "案その2", "subtitle": "サブその2"},
           {"title": "案その3", "subtitle": "サブその3"}]


def _set_options(release):
    release.title_candidates = json.dumps(OPTIONS, ensure_ascii=False)
    release.summary_source = "ai_assisted"
    db.session.commit()


def test_admin_sees_three_title_sets_and_the_chat(admin_client, pending):
    from app.models import AIChatMessage

    _set_options(pending)
    db.session.add(AIChatMessage(release_id=pending.id, role="client", content="新しい和菓子です"))
    db.session.add(AIChatMessage(release_id=pending.id, role="ai", content="どこが新しいですか？"))
    db.session.commit()
    body = admin_client.get(f"/admin/releases/{pending.id}").get_data(as_text=True)
    for expected in ("AI の案（タイトル＋サブタイトルの3セット）", "案その1", "サブその1", "案その2", "サブその2", "案その3",
                     "サブその3", "この案を使う", "新しい和菓子です", "どこが新しいですか？", "AI が、クライアントとのチャットから"):
        assert expected in body, expected
    assert body.count(">この案を使う</button>") == 3  # ボタンは、3つ
    for i in range(3):
        assert f"/admin/releases/{pending.id}/title-options/{i}" in body


def test_use_this_title_set_fills_title_and_subtitle_together(admin_client, pending, admin_user):
    _set_options(pending)
    res = admin_client.post(f"/admin/releases/{pending.id}/title-options/1", follow_redirects=True)
    assert res.status_code == 200 and "この案を、タイトルとサブタイトルに入れました。" in res.get_data(as_text=True)
    db.session.refresh(pending)
    assert (pending.title, pending.subtitle) == ("案その2", "サブその2")
    assert pending.status == "pending" and pending.last_edited_by == admin_user.id
    assert {(e.field_name, e.new_value) for e in pending.edits} == {("title", "案その2"), ("subtitle", "サブその2")}
    body = admin_client.get(f"/admin/releases/{pending.id}").get_data(as_text=True)
    assert 'value="案その2"' in body and 'value="サブその2"' in body  # フォームに入っている


def test_use_title_set_keeps_the_summary_and_can_be_redone(admin_client, pending):
    _set_options(pending)
    pending.summary = "書いた要旨"
    db.session.commit()
    admin_client.post(f"/admin/releases/{pending.id}/title-options/0")
    admin_client.post(f"/admin/releases/{pending.id}/title-options/2")
    db.session.refresh(pending)
    assert (pending.title, pending.subtitle, pending.summary) == ("案その3", "サブその3", "書いた要旨")


@pytest.mark.parametrize("index", [3, 99])
def test_use_title_set_with_an_unknown_number(admin_client, pending, index):
    _set_options(pending)
    res = admin_client.post(f"/admin/releases/{pending.id}/title-options/{index}", follow_redirects=True)
    assert "その案は、ありません。" in res.get_data(as_text=True)
    db.session.refresh(pending)
    assert pending.title == FULL["title"]


def test_use_title_set_is_only_for_pending_and_for_admins(admin_client, client, client_user, pending):
    _set_options(pending)
    login(client, "client@example.com")
    assert client.post(f"/admin/releases/{pending.id}/title-options/0").status_code == 403
    pending.status, pending.approved_at = "approved", utcnow()
    db.session.commit()
    res = admin_client.post(f"/admin/releases/{pending.id}/title-options/0", follow_redirects=True)
    assert "承認待ちの書類だけ" in res.get_data(as_text=True)
    anonymous = admin_client.application.test_client()
    assert anonymous.post(f"/admin/releases/{pending.id}/title-options/0").status_code == 302


def test_old_candidate_format_is_still_readable(app, pending):
    pending.title_candidates = "古い案その1\n古い案その2"
    assert pending.title_options == [{"title": "古い案その1", "subtitle": ""}, {"title": "古い案その2", "subtitle": ""}]
    pending.title_candidates = "{こわれた"
    assert pending.title_options == [{"title": "{こわれた", "subtitle": ""}]  # 読めなくても、壊れない
    pending.title_candidates = None
    assert pending.title_options == []


def test_admin_can_edit_the_features_heading(admin_client, pending):
    admin_client.post(f"/admin/releases/{pending.id}", data=_edit_data(features_heading="アプリの特徴"))
    db.session.refresh(pending)
    assert pending.features_heading == "アプリの特徴"
    assert [e.field_name for e in pending.edits if e.field_name == "features_heading"] == ["features_heading"]


def test_admin_cannot_edit_source_info(admin_client, pending):
    admin_client.post(f"/admin/releases/{pending.id}", data=_edit_data(source_what="書き換え"))
    db.session.refresh(pending)
    assert pending.source_what == FULL["source_what"]


def test_admin_cannot_edit_non_pending(admin_client, client_user):
    r = _pending_release(client_user)
    r.status = "approved"
    db.session.commit()
    admin_client.post(f"/admin/releases/{r.id}", data=_edit_data(title="変更"))
    db.session.refresh(r)
    assert r.title == FULL["title"]


def test_admin_photo_add_and_delete_recorded(admin_client, pending):
    admin_client.post(f"/admin/releases/{pending.id}/photos",
                      data={"photo": (io.BytesIO(png_bytes()), "管理者追加.png")}, content_type="multipart/form-data")
    db.session.refresh(pending)
    assert len(pending.photos) == 1
    photo = pending.photos[0]
    path = resolve_path(photo.file_path)
    admin_client.post(f"/admin/releases/{pending.id}/photos/{photo.id}/delete")
    db.session.refresh(pending)
    assert pending.photos == [] and not path.exists()
    assert [e.field_name for e in pending.edits] == ["photos", "photos"]


# ---- 承認 ----
def test_approve_requires_summary(admin_client, pending):
    res = admin_client.post(f"/admin/releases/{pending.id}/approve", follow_redirects=True)
    assert "要旨" in res.get_data(as_text=True)
    db.session.refresh(pending)
    assert pending.status == "pending" and pending.file_path is None


def _approve(admin_client, release):
    admin_client.post(f"/admin/releases/{release.id}", data=_edit_data())
    return admin_client.post(f"/admin/releases/{release.id}/approve")


def test_approve_creates_pdf_and_sets_fields(admin_client, pending, admin_user):
    assert _approve(admin_client, pending).status_code == 302
    db.session.refresh(pending)
    assert pending.status == "approved"
    assert pending.approved_by == admin_user.id and pending.approved_at is not None
    path = resolve_path(pending.file_path)
    assert path.is_file()
    assert path.name == "release.pdf" and path.read_bytes().startswith(b"%PDF-")
    text = _pdf_text(path)
    assert "管理者が書いた要旨です。" in text and FULL["title"] in text


def test_approve_rolls_back_when_pdf_fails(admin_client, pending):
    admin_client.post(f"/admin/releases/{pending.id}", data=_edit_data())
    with patch("app.pdfgen.write_release", side_effect=RuntimeError("boom")):
        res = admin_client.post(f"/admin/releases/{pending.id}/approve", follow_redirects=True)
    assert "承認できませんでした" in res.get_data(as_text=True)
    db.session.refresh(pending)
    assert pending.status == "pending"
    assert pending.approved_by is None and pending.approved_at is None and pending.file_path is None


def test_approve_twice_is_rejected(admin_client, pending):
    _approve(admin_client, pending)
    db.session.refresh(pending)
    first_path = pending.file_path
    res = admin_client.post(f"/admin/releases/{pending.id}/approve", follow_redirects=True)
    assert "承認待ちの書類だけ" in res.get_data(as_text=True)
    db.session.refresh(pending)
    assert pending.file_path == first_path


def test_approved_release_is_locked_for_admin_edits(admin_client, pending):
    _approve(admin_client, pending)
    admin_client.post(f"/admin/releases/{pending.id}", data=_edit_data(title="承認後の変更"))
    db.session.refresh(pending)
    assert pending.title == FULL["title"]


# ---- 差し戻し ----
def test_reject_requires_reason(admin_client, pending):
    res = admin_client.post(f"/admin/releases/{pending.id}/reject", data={"reason": "  "}, follow_redirects=True)
    assert "理由" in res.get_data(as_text=True)
    db.session.refresh(pending)
    assert pending.status == "pending"


def test_reject_then_client_edits_and_resubmits(admin_client, client, client_user, pending):
    admin_client.post(f"/admin/releases/{pending.id}/reject", data={"reason": "写真を足してください"})
    db.session.refresh(pending)
    assert pending.status == "rejected" and pending.reject_reason == "写真を足してください"
    login(client, "client@example.com")
    assert "写真を足してください" in client.get(f"/releases/{pending.id}/edit").get_data(as_text=True)
    client.post(f"/releases/{pending.id}/edit", data=form_data({**{k: FULL[k] for k in FORM_FIELDS}, "location": "直した会場"}))
    assert client.post(f"/releases/{pending.id}/submit").status_code == 302
    db.session.refresh(pending)
    assert pending.status == "pending" and pending.location == "直した会場"


# ---- ダウンロード ----
def test_client_can_download_only_after_approval(admin_client, client, client_user, pending):
    login(client, "client@example.com")
    assert client.get(f"/releases/{pending.id}/download").status_code == 404  # 承認前
    _approve(admin_client, pending)
    res = client.get(f"/releases/{pending.id}/download")
    assert res.status_code == 200
    assert "attachment" in res.headers["Content-Disposition"] and res.mimetype == "application/pdf"
    assert res.data.startswith(b"%PDF-") and FULL["title"] in _pdf_text(res.data)  # 開ける PDF


def test_other_client_cannot_download(app, admin_client, pending):
    _approve(admin_client, pending)
    other = app.test_client()
    make_user(email="other@example.com")
    login(other, "other@example.com")
    assert other.get(f"/releases/{pending.id}/download").status_code == 404


def test_admin_download_and_anonymous_blocked(app, admin_client, pending):
    assert admin_client.get(f"/admin/releases/{pending.id}/download").status_code == 404  # 承認前
    _approve(admin_client, pending)
    assert admin_client.get(f"/admin/releases/{pending.id}/download").status_code == 200
    assert app.test_client().get(f"/admin/releases/{pending.id}/download").status_code == 302


def test_preview_pdf_does_not_approve_or_save(admin_client, pending):
    admin_client.post(f"/admin/releases/{pending.id}", data=_edit_data())
    res = admin_client.get(f"/admin/releases/{pending.id}/preview.pdf")
    assert res.status_code == 200 and res.mimetype == "application/pdf"
    assert "attachment" not in res.headers.get("Content-Disposition", "")  # ブラウザの中で開く（プレビュー）
    assert "管理者が書いた要旨です。" in _pdf_text(res.data)
    db.session.refresh(pending)
    assert pending.status == "pending" and pending.file_path is None


def test_preview_pdf_only_for_pending(admin_client, pending):
    _approve(admin_client, pending)
    assert admin_client.get(f"/admin/releases/{pending.id}/preview.pdf").status_code == 404


# ---- メール通知 ----
def _mails(app):
    return app.extensions.get("mail_outbox", [])


def test_submit_notifies_admins(app, client, client_user, admin_user):
    login(client, "client@example.com")
    client.post("/releases/new")
    r = Release.query.one()
    client.post(f"/releases/{r.id}/edit", data=form_data({k: FULL[k] for k in FORM_FIELDS}))
    for name in ("feature_1", "source_what", "source_why", "source_message"):  # AI とのチャットで入る内容
        setattr(r, name, FULL[name])
    db.session.commit()
    client.post(f"/releases/{r.id}/submit")
    mails = [m for m in _mails(app) if m["to"] == "admin@example.com"]
    assert len(mails) == 1 and "承認待ち" in mails[0]["subject"]
    assert f"/admin/releases/{r.id}" in mails[0]["body"]


def test_approve_notifies_client_with_changes_but_not_summary(app, admin_client, client_user, pending):
    admin_client.post(f"/admin/releases/{pending.id}", data=_edit_data(title="管理者が直したタイトル"))
    admin_client.post(f"/admin/releases/{pending.id}/approve")
    mail = next(m for m in _mails(app) if m["to"] == "client@example.com")
    assert "承認されました" in mail["subject"] + mail["body"]
    assert "管理者が直したタイトル" in mail["body"] and FULL["title"] in mail["body"]  # 変更前後
    assert "管理者が書いた要旨です。" not in mail["body"]  # 要旨は変更点に含めない


def test_approve_mail_without_changes_has_no_change_section(app, admin_client, client_user, pending):
    admin_client.post(f"/admin/releases/{pending.id}", data=_edit_data())  # 要旨だけ
    admin_client.post(f"/admin/releases/{pending.id}/approve")
    mail = next(m for m in _mails(app) if m["to"] == "client@example.com")
    assert "修正しました" not in mail["body"]


def test_reject_notifies_client_with_reason(app, admin_client, client_user, pending):
    admin_client.post(f"/admin/releases/{pending.id}/reject", data={"reason": "日時を確認してください"})
    mail = next(m for m in _mails(app) if m["to"] == "client@example.com")
    assert "差し戻され" in mail["subject"] and "日時を確認してください" in mail["body"]


def test_client_sees_admin_changes_on_approved_release(app, admin_client, client, client_user, pending):
    admin_client.post(f"/admin/releases/{pending.id}", data=_edit_data(title="管理者が直したタイトル"))
    admin_client.post(f"/admin/releases/{pending.id}/approve")
    login(client, "client@example.com")
    body = client.get(f"/releases/{pending.id}").get_data(as_text=True)
    assert "管理者が修正した内容" in body and "管理者が直したタイトル" in body
    assert "管理者が書いた要旨です。" in body  # 要旨そのものは表示される


def test_admin_date_change_is_recorded_in_edit_history(admin_client, pending):
    res = admin_client.post(f"/admin/releases/{pending.id}", data=_edit_data(event_date="2026-12-08"))
    assert res.status_code == 302
    db.session.refresh(pending)
    from datetime import date
    assert pending.event_date == date(2026, 12, 8)
    rows = {e.field_name: (e.old_value, e.new_value) for e in pending.edits}
    assert rows["event_date"] == ("2026年12月1日（火） 14:00〜16:00", "2026年12月8日（火） 14:00〜16:00")
    assert "event_start_time" not in rows


def test_admin_saving_without_date_change_records_nothing(admin_client, pending):
    admin_client.post(f"/admin/releases/{pending.id}", data=_edit_data())
    db.session.refresh(pending)
    assert [e.field_name for e in pending.edits] == ["summary"]
