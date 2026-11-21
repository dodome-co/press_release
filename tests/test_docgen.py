import io

import pytest
from docx import Document

from app import docgen
from app.extensions import db
from app.models import Release, ReleasePhoto, ReleaseScheduleItem, utcnow
from app.uploads import save_image

from .conftest import make_user, png_bytes
from .test_releases import FULL


def _release(user, **overrides):
    values = {"summary": "要旨です。", "approved_at": utcnow(), **FULL, **overrides}
    r = Release(user_id=user.id, **values)
    db.session.add(r)
    db.session.commit()
    return r


def _doc(release):
    return Document(io.BytesIO(docgen.render_release(release)))


def _text(doc):
    parts = [p.text for p in doc.paragraphs]
    for t in doc.tables:
        for row in t.rows:
            parts.extend(c.text for c in row.cells)
    return "\n".join(parts)


def _storage(data=None):
    from werkzeug.datastructures import FileStorage

    return FileStorage(stream=io.BytesIO(data or png_bytes()), filename="p.png")


def test_basic_fields_are_filled(app, client_user):
    text = _text(_doc(_release(client_user)))
    for expected in ("報道関係者各位", FULL["title"], "要旨です。", "2026年12月1日（火） 14:00〜16:00",
                     FULL["location"], "◆ 商品の特徴", "・速い", "株式会社テスト",
                     "〒123-4567 東京都千代田区1-1-1", "TEL：03-1234-5678", "E-mail：client@example.com"):
        assert expected in text, expected


def test_word_has_no_application_schedule_or_description(app, client_user):
    """よいプレスリリースの定義：取材の申し込み、当日のスケジュール、サービスの概要は、載せない。"""
    r = _release(client_user)
    r.schedule_items.append(ReleaseScheduleItem(position=1, time_text="14:00", content="説明"))
    db.session.commit()
    text = _text(_doc(r))
    for gone in ("取材のお申し込み", "ご来場が難しくなった場合", "締切", "当日のスケジュール", "◆ サービスの内容",
                 FULL["service_description"], FULL["application_method"], "14:00 説明"):
        assert gone not in text, gone


def test_subtitle_and_speakers_are_shown(app, client_user):
    r = _release(client_user, subtitle="地域の農家と連携", speakers="社長　岩手　盛子\n栗生産者　花巻　賢治")
    doc = _doc(r)
    text = _text(doc)
    for expected in ("～地域の農家と連携～", "◆ 当日の対応者", "社長　岩手　盛子", "栗生産者　花巻　賢治"):
        assert expected in text, expected
    lines = text.splitlines()
    assert lines.index(FULL["title"]) < lines.index("～地域の農家と連携～") < lines.index("要旨です。")
    assert lines.index("◆ 日時・会場") < lines.index("◆ 当日の対応者") < lines.index("◆ 商品の特徴")


def test_subtitle_and_speakers_are_omitted_when_empty(app, client_user):
    text = _text(_doc(_release(client_user)))
    assert "◆ 当日の対応者" not in text and "～～" not in text


def test_features_heading_is_omitted_without_features(app, client_user):
    text = _text(_doc(_release(client_user, feature_1="", feature_2="", feature_3="")))
    assert "◆ 商品の特徴" not in text


def test_no_leftover_template_tags(app, client_user):
    text = _text(_doc(_release(client_user)))
    assert "{{" not in text and "{%" not in text and "}}" not in text


def test_empty_sections_are_omitted(app, client_user):
    text = _text(_doc(_release(client_user)))
    assert "◆ 写真" not in text  # 写真なし
    assert "携帯" not in text  # 携帯電話番号なし
    for label in ("設立：", "資本金：", "従業員数：", "事業内容：", "URL："):
        assert label not in text  # 会社概要の空の項目は行ごと消える


def test_optional_parts_shown_when_present(app):
    user = make_user(email="full@example.com", mobile_phone="090-1111-2222", founded="2015年4月",
                     capital="1,000万円", employees="25名", business_description="ソフト開発",
                     website_url="https://example.com", department="広報部")
    r = _release(user)
    text = _text(_doc(r))
    for expected in ("（携帯：090-1111-2222）", "設立：2015年4月",
                     "資本金：1,000万円", "従業員数：25名", "事業内容：ソフト開発", "URL：https://example.com", "広報部"):
        assert expected in text, expected


def test_features_only_nonempty(app, client_user):
    text = _text(_doc(_release(client_user, feature_1="A", feature_2="", feature_3="C")))
    bullets = [line for line in text.splitlines() if line.startswith("・")]
    assert bullets == ["・A", "・C"]


def test_photos_and_logo_are_embedded(app, client_user):
    r = _release(client_user)
    for pos in (1, 2):
        saved = save_image(_storage(), f"releases/{r.id}/photos")
        db.session.add(ReleasePhoto(release_id=r.id, position=pos, file_path=saved.relative_path,
                                    original_filename="p.png", content_type=saved.content_type,
                                    size_bytes=saved.size_bytes))
    client_user.logo_path = save_image(_storage(), f"users/{client_user.id}/logo").relative_path
    db.session.commit()
    doc = _doc(r)
    assert len(doc.inline_shapes) == 3  # 写真2枚 + ロゴ1枚
    assert "◆ 写真" not in _text(doc)  # 写真は、見出しなしで最後の余白に置く


def test_missing_photo_file_is_skipped_not_fatal(app, client_user):
    r = _release(client_user)
    db.session.add(ReleasePhoto(release_id=r.id, position=1, file_path="releases/x/photos/gone.png",
                                original_filename="p.png", content_type="image/png", size_bytes=1))
    db.session.commit()
    assert len(_doc(r).inline_shapes) == 0


def test_special_characters_are_kept_as_text(app, client_user):
    r = _release(client_user, title="A & B <テスト> {{ 7*7 }}")
    text = _text(_doc(r))
    assert "A & B <テスト> {{ 7*7 }}" in text and "49" not in text  # 記号で壊れず、式としても動かない


def test_issued_date_is_approval_date(app, client_user):
    from datetime import datetime

    r = _release(client_user)
    r.approved_at = datetime(2026, 12, 1, 10, 0)
    db.session.commit()
    assert "2026年12月1日" in _text(_doc(r))


def test_missing_template_raises(app, client_user, tmp_path):
    app.config["WORD_TEMPLATE_PATH"] = str(tmp_path / "nothing.docx")
    with pytest.raises(FileNotFoundError):
        docgen.render_release(_release(client_user))


def test_write_release_leaves_no_temp_files(app, client_user):
    r = _release(client_user)
    relative = docgen.write_release(r)
    from app.uploads import resolve_path

    folder = resolve_path(relative).parent
    assert [p.name for p in folder.iterdir()] == ["release.docx"]


def test_newlines_become_line_breaks_in_word(app, client_user):
    import zipfile

    r = _release(client_user, summary="1行目\n2行目")
    xml = zipfile.ZipFile(io.BytesIO(docgen.render_release(r))).read("word/document.xml").decode()
    assert "1行目</w:t><w:br/><w:t" in xml  # 改行が Word の改行になる


def test_xml_special_characters_do_not_corrupt_file(app, client_user):
    r = _release(client_user, title="</w:t></w:p><w:p>壊れる？", summary="&amp; <b>太字</b>")
    doc = _doc(r)  # 開けること自体が確認
    text = _text(doc)
    assert "</w:t></w:p><w:p>壊れる？" in text and "&amp; <b>太字</b>" in text


def test_title_is_centered(app, client_user):
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    doc = _doc(_release(client_user))
    title = next(p for p in doc.paragraphs if p.text == FULL["title"])
    assert title.alignment == WD_ALIGN_PARAGRAPH.CENTER


def _drawings_per_paragraph(doc):
    from docx.oxml.ns import qn

    return [len(p._p.findall(".//" + qn("w:drawing"))) for p in doc.paragraphs]


def test_photos_sit_side_by_side_at_the_end(app, client_user):
    r = _release(client_user)
    for pos in (1, 2):
        saved = save_image(_storage(), f"releases/{r.id}/photos")
        db.session.add(ReleasePhoto(release_id=r.id, position=pos, file_path=saved.relative_path,
                                    original_filename="p.png", content_type=saved.content_type,
                                    size_bytes=saved.size_bytes))
    db.session.commit()
    doc = _doc(r)
    counts = _drawings_per_paragraph(doc)
    assert counts.count(2) == 1  # 2枚が同じ段落（横並び）
    index = counts.index(2)
    texts = [p.text for p in doc.paragraphs]
    assert any(t.startswith("E-mail：") for t in texts[:index])  # お問い合わせ先より後ろ
    assert not any(texts[index + 1 :])  # その後ろには、何も続かない


def test_single_photo_is_placed_at_the_end_too(app, client_user):
    r = _release(client_user)
    saved = save_image(_storage(), f"releases/{r.id}/photos")
    db.session.add(ReleasePhoto(release_id=r.id, position=1, file_path=saved.relative_path,
                                original_filename="p.png", content_type=saved.content_type,
                                size_bytes=saved.size_bytes))
    db.session.commit()
    assert _drawings_per_paragraph(_doc(r)).count(1) == 1


def test_page_is_a4(app, client_user):
    from docx.shared import Mm

    section = _doc(_release(client_user)).sections[0]
    assert round(section.page_width.mm) == 210 and round(section.page_height.mm) == 297
