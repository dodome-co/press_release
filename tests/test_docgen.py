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
    for expected in ("報道関係者各位", f"【取材のお願い】{FULL['title']}", "要旨です。", FULL["event_at_text"],
                     FULL["location"], FULL["service_description"], "・速い", FULL["application_method"],
                     FULL["application_deadline"], "お問い合わせ先までご連絡ください", "株式会社テスト",
                     "〒123-4567 東京都千代田区1-1-1", "TEL：03-1234-5678", "E-mail：client@example.com"):
        assert expected in text, expected


def test_no_leftover_template_tags(app, client_user):
    text = _text(_doc(_release(client_user)))
    assert "{{" not in text and "{%" not in text and "}}" not in text


def test_empty_sections_are_omitted(app, client_user):
    text = _text(_doc(_release(client_user)))
    assert "当日のスケジュール" not in text  # スケジュールなし
    assert "◆ 写真" not in text  # 写真なし
    assert "携帯" not in text  # 携帯電話番号なし
    for label in ("設立：", "資本金：", "従業員数：", "事業内容：", "URL："):
        assert label not in text  # 会社概要の空の項目は行ごと消える


def test_optional_parts_shown_when_present(app):
    user = make_user(email="full@example.com", mobile_phone="090-1111-2222", founded="2015年4月",
                     capital="1,000万円", employees="25名", business_description="ソフト開発",
                     website_url="https://example.com", department="広報部")
    r = _release(user)
    r.schedule_items.append(ReleaseScheduleItem(position=1, time_text="14:00", content="説明"))
    r.schedule_items.append(ReleaseScheduleItem(position=2, time_text="15:00", content="デモ"))
    db.session.commit()
    text = _text(_doc(r))
    for expected in ("当日のスケジュール", "14:00", "デモ", "（携帯：090-1111-2222）", "設立：2015年4月",
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
    assert "◆ 写真" in _text(doc)


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

    r = _release(client_user, service_description="1行目\n2行目")
    xml = zipfile.ZipFile(io.BytesIO(docgen.render_release(r))).read("word/document.xml").decode()
    assert "1行目</w:t><w:br/><w:t" in xml  # 改行が Word の改行になる


def test_xml_special_characters_do_not_corrupt_file(app, client_user):
    r = _release(client_user, title="</w:t></w:p><w:p>壊れる？", summary="&amp; <b>太字</b>")
    doc = _doc(r)  # 開けること自体が確認
    text = _text(doc)
    assert "</w:t></w:p><w:p>壊れる？" in text and "&amp; <b>太字</b>" in text
