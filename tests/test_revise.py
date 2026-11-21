"""承認されたあと、ダウンロードする前に、クライアントが、PDF をプレビューし、内容を直せる。"""
import io
import re
from unittest.mock import patch

import pytest
from pypdf import PdfReader

from app.extensions import db
from app.models import Release, utcnow
from app.releases import service
from app.uploads import resolve_path

from .conftest import login, make_user
from .test_admin_releases import _edit_data, _pdf_text, _pending_release


@pytest.fixture()
def admin_client(app, admin_user):
    c = app.test_client()
    login(c, "admin@example.com")
    return c


@pytest.fixture()
def approved(admin_client, client_user):
    release = _pending_release(client_user)
    admin_client.post(f"/admin/releases/{release.id}", data=_edit_data())
    admin_client.post(f"/admin/releases/{release.id}/approve")
    db.session.refresh(release)
    assert release.status == "approved"
    return release


@pytest.fixture()
def me(client, client_user):
    login(client, "client@example.com")
    return client


def _revise_data(release, **overrides):
    data = {"title": release.title, "subtitle": release.subtitle or "", "summary": release.summary or "",
            "event_date": release.event_date.isoformat(), "event_start_time": release.event_start_time.strftime("%H:%M"),
            "event_end_time": release.event_end_time.strftime("%H:%M") if release.event_end_time else "",
            "location": release.location, "speakers": release.speakers or "",
            "features_heading": release.features_heading or "", "feature_1": release.feature_1, "feature_2": release.feature_2, "feature_3": release.feature_3}
    data.update(overrides)
    return data


# ---- 承認後の画面：プレビュー → 修正 → ダウンロード ----
def test_approved_view_shows_preview_revise_download_in_order(me, approved):
    body = me.get(f"/releases/{approved.id}").get_data(as_text=True)
    assert "承認されました。ダウンロードの前に、仕上がりを確かめて、必要なら直してください。" in body
    a, b, c = (body.index(x) for x in (f"/releases/{approved.id}/preview.pdf", f"/releases/{approved.id}/revise",
                                       f"/releases/{approved.id}/download"))
    assert a < b < c
    assert "PDF をプレビューする" in body and "内容を修正する" in body and "PDF をダウンロードする" in body


def test_mypage_has_preview_revise_and_download(me, approved):
    body = me.get("/mypage/").get_data(as_text=True)
    for path in ("preview.pdf", "revise", "download"):
        assert f"/releases/{approved.id}/{path}" in body


# ---- プレビュー（PDF をブラウザで開く） ----
def test_preview_pdf_opens_in_the_browser_with_the_current_content(me, approved):
    res = me.get(f"/releases/{approved.id}/preview.pdf")
    assert res.status_code == 200 and res.mimetype == "application/pdf"
    assert "attachment" not in res.headers.get("Content-Disposition", "")  # ダウンロードではなく、ブラウザの中で開く
    assert "no-store" in res.headers["Cache-Control"]  # ブラウザに保存させない
    assert FULL_TITLE in _pdf_text(res.data)


FULL_TITLE = "新サービス「テスト」開始"


def test_preview_pdf_works_before_approval_too(me, client_user, app):
    release = _pending_release(client_user)
    assert me.get(f"/releases/{release.id}/preview.pdf").status_code == 200
    assert "いまの内容で、PDF のプレビューを見る" in me.get(f"/releases/{release.id}").get_data(as_text=True)


def test_preview_pdf_is_private(app, approved, me):
    anonymous = app.test_client()
    assert anonymous.get(f"/releases/{approved.id}/preview.pdf").status_code == 302
    other = app.test_client()
    make_user(email="other@example.com")
    login(other, "other@example.com")
    assert other.get(f"/releases/{approved.id}/preview.pdf").status_code == 404


# ---- 修正 ----
def test_revise_form_is_filled_with_the_current_content(me, approved):
    body = me.get(f"/releases/{approved.id}/revise").get_data(as_text=True)
    assert 'name="title"' in body and FULL_TITLE in body and "管理者が書いた要旨です。" in body
    assert "いまの内容は、A4 1枚に収まっています。" in body


def test_revise_changes_the_content_and_remakes_the_pdf(me, approved, client_user):
    approved_at, path_before = approved.approved_at, approved.file_path
    res = me.post(f"/releases/{approved.id}/revise", data=_revise_data(
        approved, title="直したタイトル", summary="直した要旨です。", speakers="社長　岩手　盛子"), follow_redirects=True)
    assert res.status_code == 200 and "直しました。PDF を作り直しました。" in res.get_data(as_text=True)
    db.session.refresh(approved)
    assert approved.title == "直したタイトル" and approved.summary == "直した要旨です。"
    assert approved.status == "approved" and approved.approved_at == approved_at  # 承認は、そのまま
    assert approved.file_path == path_before
    text = _pdf_text(resolve_path(approved.file_path))
    assert "直したタイトル" in text and "直した要旨です。" in text and "社長岩手盛子" in text  # PDF が作り直された
    assert FULL_TITLE not in text
    # ダウンロードも、直した内容
    assert "直したタイトル" in _pdf_text(me.get(f"/releases/{approved.id}/download").data)


def test_revise_is_recorded_in_the_edit_history_with_who_changed_it(me, approved, client_user, admin_client):
    me.post(f"/releases/{approved.id}/revise", data=_revise_data(approved, title="直したタイトル"))
    db.session.refresh(approved)
    edits = [e for e in approved.edits if e.edited_by == client_user.id]  # 管理者が承認前に直した分は、含めない
    assert [(e.field_name, e.old_value, e.new_value) for e in edits] == [("title", FULL_TITLE, "直したタイトル")]
    assert approved.last_edited_by == client_user.id
    body = admin_client.get(f"/admin/releases/{approved.id}").get_data(as_text=True)  # 管理者も、履歴で見られる
    assert "編集履歴" in body and "直したタイトル" in body


def test_revise_without_changes_does_nothing(me, approved):
    path = resolve_path(approved.file_path)
    before = path.read_bytes()
    res = me.post(f"/releases/{approved.id}/revise", data=_revise_data(approved), follow_redirects=True)
    assert "変更はありませんでした。" in res.get_data(as_text=True)
    assert path.read_bytes() == before


def test_revise_has_no_title_length_limit_but_stops_the_absurd(me, approved):
    long_title = "あ" * 40  # 22文字を超えても、よい（字数の制限は、なくした）
    assert me.post(f"/releases/{approved.id}/revise", data=_revise_data(approved, title=long_title)).status_code == 302
    db.session.refresh(approved)
    assert approved.title == long_title
    res = me.post(f"/releases/{approved.id}/revise", data=_revise_data(approved, title="あ" * 201))  # 保存できる長さ（200）まで
    assert res.status_code == 200
    db.session.refresh(approved)
    assert approved.title == long_title


def test_revise_can_change_the_features_heading(me, approved):
    me.post(f"/releases/{approved.id}/revise", data=_revise_data(approved, features_heading="アプリの特徴"))
    db.session.refresh(approved)
    assert approved.features_heading == "アプリの特徴"
    assert "◆アプリの特徴" in _pdf_text(resolve_path(approved.file_path))


def test_revise_rolls_back_when_the_pdf_cannot_be_made(me, approved):
    before = resolve_path(approved.file_path).read_bytes()
    edits_before = len(approved.edits)
    with patch("app.pdfgen.write_release", side_effect=RuntimeError("boom")):
        res = me.post(f"/releases/{approved.id}/revise", data=_revise_data(approved, title="直したタイトル"),
                      follow_redirects=True)
    assert "修正できませんでした" in res.get_data(as_text=True)
    db.session.refresh(approved)
    assert approved.title == FULL_TITLE and len(approved.edits) == edits_before  # 修正は、取り消される
    assert resolve_path(approved.file_path).read_bytes() == before


def test_revise_warns_when_it_does_not_fit_one_page(me, approved):
    approved.summary = "長すぎる要旨です。" * 400  # フォームの上限（2000文字）を超える長さを、直接入れる
    db.session.commit()
    body = me.get(f"/releases/{approved.id}/revise").get_data(as_text=True)
    assert "A4 1枚に収まっていません" in body


# ---- 直せるのは、承認されたあとの、自分の書類だけ ----
@pytest.mark.parametrize("status", ["draft", "pending", "rejected"])
def test_revise_is_only_for_approved_releases(me, client_user, status):
    release = _pending_release(client_user)
    release.status = status
    db.session.commit()
    assert me.get(f"/releases/{release.id}/revise").status_code == 404
    assert me.post(f"/releases/{release.id}/revise", data=_revise_data(release)).status_code == 404


def test_revise_is_private(app, approved):
    anonymous = app.test_client()
    assert anonymous.get(f"/releases/{approved.id}/revise").status_code == 302
    other = app.test_client()
    make_user(email="other@example.com")
    login(other, "other@example.com")
    assert other.get(f"/releases/{approved.id}/revise").status_code == 404
    assert other.post(f"/releases/{approved.id}/revise", data=_revise_data(approved)).status_code == 404


def test_admin_cannot_use_the_client_revise(admin_client, approved):
    assert admin_client.get(f"/releases/{approved.id}/revise").status_code == 403


def test_service_refuses_to_revise_a_release_that_is_not_approved(client_user):
    release = _pending_release(client_user)
    with pytest.raises(service.ReleaseError, match="承認された書類だけ"):
        service.client_revise(release, client_user, {"title": "x"}, "要旨")
