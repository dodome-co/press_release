"""プレスリリースの PDF 出力（よいプレスリリースの定義の形・A4 1枚・日本語フォントの埋め込み）。"""
import io
import re

import pytest
from pypdf import PdfReader

from app import pdfgen
from app.extensions import db
from app.models import Release, ReleasePhoto, utcnow
from app.uploads import save_image

from .conftest import make_user, png_bytes
from .test_releases import FULL


def _release(user, **overrides):
    values = {"summary": "要旨です。", "approved_at": utcnow(), **FULL, **overrides}
    r = Release(user_id=user.id, **values)
    db.session.add(r)
    db.session.commit()
    return r


def _render(release):
    return pdfgen.render_release(release)


def _read(rendered):
    return PdfReader(io.BytesIO(rendered.data))


def _text(rendered) -> str:
    """PDF の文字（改行・空白を除いたもの。折り返しの位置に左右されないように）。"""
    return re.sub(r"\s+", "", "".join(page.extract_text() for page in _read(rendered).pages))


def _storage(data=None):
    from werkzeug.datastructures import FileStorage

    return FileStorage(stream=io.BytesIO(data or png_bytes()), filename="p.png")


def _add_photos(release, count=2):
    for pos in range(1, count + 1):
        # 同じ画像は、PDF が1つにまとめてしまう。数えられるよう、色を変える
        saved = save_image(_storage(png_bytes(color=(40 * pos, 90, 160))), f"releases/{release.id}/photos")
        db.session.add(ReleasePhoto(release_id=release.id, position=pos, file_path=saved.relative_path,
                                    original_filename="p.png", content_type=saved.content_type,
                                    size_bytes=saved.size_bytes))
    db.session.commit()


def _images(rendered) -> int:
    return sum(len(page.images) for page in _read(rendered).pages)


def test_basic_fields_are_filled(app, client_user):
    text = _text(_render(_release(client_user)))
    for expected in ("報道関係者各位", FULL["title"], "要旨です。", "2026年12月1日（火）14:00〜16:00", FULL["location"].replace(" ", ""),
                     "◆商品の特徴", "・速い", "株式会社テスト", "〒123-4567東京都千代田区1-1-1", "TEL：03-1234-5678",
                     "E-mail：client@example.com", "株式会社テストプレスリリース"):
        assert expected in text, expected


def test_it_is_a_pdf_on_one_a4_page(app, client_user):
    rendered = _render(_release(client_user))
    assert rendered.data.startswith(b"%PDF-") and rendered.pages == 1
    page = _read(rendered).pages[0]
    assert round(float(page.mediabox.width)) == 595 and round(float(page.mediabox.height)) == 842  # A4


def test_japanese_font_is_embedded(app, client_user):
    """見る人のパソコンにフォントがなくても、読めるように、フォントを埋め込む。"""
    data = _render(_release(client_user)).data
    assert b"BIZUDPGothic" in data and b"/FontFile2" in data


def test_guide_heading_has_no_square_and_is_centered(app, client_user):
    """「取材のご案内」は、■を付けず、真ん中に寄せる。"""
    rendered = _render(_release(client_user))
    text = _text(rendered)
    assert "取材のご案内" in text and "■" not in text
    page = _read(rendered).pages[0]
    spans = []
    page.extract_text(visitor_text=lambda t, cm, tm, fd, fs: spans.append((t.strip(), tm[4] * cm[0] + cm[4])) if t.strip() == "取材のご案内" else None)
    assert spans, "見出しが見つからない"
    x = spans[0][1]
    assert 200 < x < 300  # 見出しの左端が、ページ（幅595）の真ん中のあたり（左端の余白 51 ではない）


def test_no_application_schedule_description_or_old_prefix(app, client_user):
    """よいプレスリリースの定義：取材の申し込み、当日のスケジュール、サービスの概要、【取材のお願い】は、載せない。"""
    text = _text(_render(_release(client_user)))
    for gone in ("取材のお申し込み", "締切", "当日のスケジュール", "◆サービスの内容", "【取材のお願い】",
                 FULL["service_description"], FULL["application_method"]):
        assert gone not in text, gone


def test_subtitle_and_speakers_are_shown_in_order(app, client_user):
    text = _text(_render(_release(client_user, subtitle="地域の農家と連携", speakers="社長　岩手　盛子\n栗生産者　花巻　賢治")))
    for expected in ("～地域の農家と連携～", "◆当日の対応者", "社長岩手盛子", "栗生産者花巻賢治"):
        assert expected in text, expected
    assert text.index(FULL["title"]) < text.index("～地域の農家と連携～") < text.index("要旨です。") < text.index("取材のご案内")
    assert text.index("◆日時・会場") < text.index("◆当日の対応者") < text.index("◆商品の特徴")


def test_empty_sections_are_omitted(app, client_user):
    text = _text(_render(_release(client_user, feature_1="", feature_2="", feature_3="")))
    for gone in ("◆当日の対応者", "～～", "◆商品の特徴", "携帯"):
        assert gone not in text, gone
    for label in ("設立：", "資本金：", "従業員数：", "事業内容：", "URL："):
        assert label not in text  # 会社概要の空の項目は、行ごと消える


def test_optional_parts_shown_when_present(app):
    user = make_user(email="full@example.com", mobile_phone="090-1111-2222", founded="2015年4月",
                     capital="1,000万円", employees="25名", business_description="ソフト開発",
                     website_url="https://example.com", department="広報部")
    text = _text(_render(_release(user)))
    for expected in ("（携帯：090-1111-2222）", "設立：2015年4月", "資本金：1,000万円", "従業員数：25名",
                     "事業内容：ソフト開発", "URL：https://example.com", "広報部"):
        assert expected in text, expected


def test_features_only_nonempty(app, client_user):
    text = _text(_render(_release(client_user, feature_1="A", feature_2="", feature_3="C")))
    assert "◆商品の特徴・A・C◆会社概要" in text  # 空の「特徴2」は、行ごと出ない


def test_photos_and_logo_are_embedded(app, client_user):
    r = _release(client_user)
    _add_photos(r, 2)
    client_user.logo_path = save_image(_storage(png_bytes(color=(200, 40, 40))), f"users/{client_user.id}/logo").relative_path
    db.session.commit()
    assert _images(_render(r)) == 3  # 写真2枚 + ロゴ1枚


def test_one_photo_and_no_photo(app, client_user):
    r = _release(client_user)
    assert _images(_render(r)) == 0
    _add_photos(r, 1)
    assert _images(_render(r)) == 1


def test_missing_photo_file_is_skipped_not_fatal(app, client_user):
    r = _release(client_user)
    db.session.add(ReleasePhoto(release_id=r.id, position=1, file_path="releases/x/photos/gone.png",
                                original_filename="p.png", content_type="image/png", size_bytes=1))
    db.session.commit()
    assert _images(_render(r)) == 0


def test_special_characters_are_kept_as_text(app, client_user):
    r = _release(client_user, title="A & B <テスト> {{ 7*7 }}", summary="&amp; <b>太字</b>")
    text = _text(_render(r))
    assert "A&B<テスト>{{7*7}}" in text and "49" not in text  # 記号で壊れず、式としても動かない
    assert "&amp;<b>太字</b>" in text  # 書いたとおりに出る（タグとして扱わない）


def test_control_characters_do_not_break_the_pdf(app, client_user):
    client_user.company_name = "A&B <C> \x01社"
    r = _release(client_user, title="t\x01 & <u>", summary="x\x02")
    text = _text(_render(r))
    assert "t&<u>" in text and "A&B<C>社" in text


def test_issued_date_is_approval_date(app, client_user):
    from datetime import datetime

    r = _release(client_user)
    r.approved_at = datetime(2026, 12, 1, 10, 0)
    db.session.commit()
    assert "2026年12月1日" in _text(_render(r))


def test_text_shrinks_to_fit_one_page(app, client_user):
    """長い要旨でも、A4 1枚に収まるまで、文字を小さくする。"""
    r = _release(client_user, summary="これはとても長い要旨です。" * 60)
    _add_photos(r, 2)
    rendered = _render(r)
    assert rendered.pages == 1
    short = _render(_release(client_user, summary="短い要旨です。"))
    assert len(rendered.data) != len(short.data)


def test_too_long_content_reports_more_than_one_page(app, client_user):
    r = _release(client_user, summary="長すぎる要旨です。" * 400)
    assert _render(r).pages > 1  # 収まらないときは、そのまま、複数ページ。ページ数で分かる


def test_write_release_leaves_no_temp_files(app, client_user):
    from app.uploads import resolve_path

    r = _release(client_user)
    relative = pdfgen.write_release(r)
    assert relative == f"releases/{r.id}/release.pdf"
    assert [p.name for p in resolve_path(relative).parent.iterdir()] == ["release.pdf"]
    assert resolve_path(relative).read_bytes().startswith(b"%PDF-")
