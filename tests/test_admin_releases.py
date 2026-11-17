import io
from unittest.mock import patch

import pytest
from docx import Document

from app.extensions import db
from app.models import Release, ReleaseEdit, ReleasePhoto, ReleaseScheduleItem, utcnow
from app.releases import service
from app.uploads import resolve_path

from .conftest import login, make_user, png_bytes
from .test_releases import FULL, form_data


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
                                              "service_description", "feature_1", "feature_2", "feature_3",
                                              "application_method", "application_deadline_at")}),
            "summary": "管理者が書いた要旨です。"}
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


def test_admin_edit_schedule_recorded(admin_client, pending):
    data = _edit_data(schedule_time_1="14:00", schedule_content_1="説明")
    admin_client.post(f"/admin/releases/{pending.id}", data=data)
    db.session.refresh(pending)
    assert [(i.time_text, i.content) for i in pending.schedule_items] == [("14:00", "説明")]
    assert [e.field_name for e in pending.edits if e.field_name == "schedule"] == ["schedule"]


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


def test_approve_creates_word_and_sets_fields(admin_client, pending, admin_user):
    assert _approve(admin_client, pending).status_code == 302
    db.session.refresh(pending)
    assert pending.status == "approved"
    assert pending.approved_by == admin_user.id and pending.approved_at is not None
    path = resolve_path(pending.file_path)
    assert path.is_file()
    text = "\n".join(p.text for p in Document(str(path)).paragraphs)
    assert "管理者が書いた要旨です。" in text and FULL["title"] in text


def test_approve_rolls_back_when_word_fails(admin_client, pending):
    admin_client.post(f"/admin/releases/{pending.id}", data=_edit_data())
    with patch("app.docgen.write_release", side_effect=RuntimeError("boom")):
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
    client.post(f"/releases/{pending.id}/edit", data=form_data({**FULL, "title": "直しました"}))
    assert client.post(f"/releases/{pending.id}/submit").status_code == 302
    db.session.refresh(pending)
    assert pending.status == "pending" and pending.title == "直しました"


# ---- ダウンロード ----
def test_client_can_download_only_after_approval(admin_client, client, client_user, pending):
    login(client, "client@example.com")
    assert client.get(f"/releases/{pending.id}/download").status_code == 404  # 承認前
    _approve(admin_client, pending)
    res = client.get(f"/releases/{pending.id}/download")
    assert res.status_code == 200
    assert "attachment" in res.headers["Content-Disposition"]
    assert Document(io.BytesIO(res.data)).paragraphs  # 開ける Word ファイル


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


def test_preview_docx_does_not_approve_or_save(admin_client, pending):
    admin_client.post(f"/admin/releases/{pending.id}", data=_edit_data())
    res = admin_client.get(f"/admin/releases/{pending.id}/preview.docx")
    assert res.status_code == 200
    assert "管理者が書いた要旨です。" in "\n".join(p.text for p in Document(io.BytesIO(res.data)).paragraphs)
    db.session.refresh(pending)
    assert pending.status == "pending" and pending.file_path is None


def test_preview_docx_only_for_pending(admin_client, pending):
    _approve(admin_client, pending)
    assert admin_client.get(f"/admin/releases/{pending.id}/preview.docx").status_code == 404


# ---- メール通知 ----
def _mails(app):
    return app.extensions.get("mail_outbox", [])


def test_submit_notifies_admins(app, client, client_user, admin_user):
    login(client, "client@example.com")
    client.post("/releases/new")
    r = Release.query.one()
    client.post(f"/releases/{r.id}/edit", data=form_data(FULL))
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
    res = admin_client.post(f"/admin/releases/{pending.id}", data=_edit_data(
        event_date="2026-12-08", application_deadline_at="2026-12-01T12:00"))
    assert res.status_code == 302
    db.session.refresh(pending)
    from datetime import date, datetime
    assert pending.event_date == date(2026, 12, 8) and pending.application_deadline_at == datetime(2026, 12, 1, 12, 0)
    rows = {e.field_name: (e.old_value, e.new_value) for e in pending.edits}
    assert rows["event_date"] == ("2026年12月1日（火） 14:00〜16:00", "2026年12月8日（火） 14:00〜16:00")
    assert rows["application_deadline_at"][1] == "2026年12月1日（火） 12:00まで"
    assert "event_start_time" not in rows


def test_admin_saving_without_date_change_records_nothing(admin_client, pending):
    admin_client.post(f"/admin/releases/{pending.id}", data=_edit_data())
    db.session.refresh(pending)
    assert [e.field_name for e in pending.edits] == ["summary"]
