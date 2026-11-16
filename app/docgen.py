"""プレスリリースの Word 出力。

運営者が作った雛形（templates/press_release.docx）に、内容を差し込む（docxtpl）。
空の項目（スケジュール、写真、特徴、会社概要の各行、携帯電話番号など）は、
雛形側の条件で、見出しや行ごと表示しない。
"""
import io
import os
import secrets
from pathlib import Path

from docx.shared import Mm
from docxtpl import DocxTemplate, InlineImage
from flask import current_app

from . import uploads
from .models import Release

TEMPLATE_NAME = "press_release.docx"


def template_path() -> Path:
    configured = current_app.config.get("WORD_TEMPLATE_PATH")
    if configured:
        return Path(configured)
    return Path(current_app.root_path).parent / "templates" / TEMPLATE_NAME


def _format_date(dt) -> str:
    return f"{dt.year}年{dt.month}月{dt.day}日" if dt else ""


def build_context(doc: DocxTemplate, release: Release) -> dict:
    """雛形に差し込む値を作る。"""
    user = release.user

    def image(relative_path, width_mm):
        if not relative_path:
            return ""
        try:
            path = uploads.resolve_path(relative_path)
        except uploads.UploadError:
            return ""
        if not path.is_file():
            return ""
        return InlineImage(doc, str(path), width=Mm(width_mm))

    photos = [image(p.file_path, 70) for p in release.photos]
    issued = release.approved_at
    if issued is None:
        from .models import utcnow

        issued = utcnow()  # 仮出力（承認前の確認）のときは、その日の日付

    company_rows = [
        {"label": label, "value": value}
        for label, value in (
            ("社名", user.company_name),
            ("設立", user.founded),
            ("資本金", user.capital),
            ("従業員数", user.employees),
            ("事業内容", user.business_description),
            ("所在地", f"〒{user.postal_code} {user.address}"),
            ("URL", user.website_url),
        )
        if value
    ]
    return {
        "logo": image(user.logo_path, 40),
        "issued_date": _format_date(issued),
        "company_name": user.company_name,
        "title": release.title,
        "summary": release.summary or "",
        "event_at": release.event_at_text,
        "location": release.location,
        "schedule": [{"time": i.time_text, "content": i.content} for i in release.schedule_items],
        "service_description": release.service_description,
        "features": release.features,
        "photos": photos,
        "application_method": release.application_method,
        "application_deadline": release.application_deadline,
        "company_rows": company_rows,
        "department": user.department or "",
        "contact_name": user.contact_name,
        "phone": user.phone,
        "mobile_phone": user.mobile_phone or "",
        "email": user.email,
    }


def render_release(release: Release) -> bytes:
    """Word ファイルの中身を作って返す（保存はしない）。"""
    path = template_path()
    if not path.is_file():
        raise FileNotFoundError(f"Word の雛形が見つかりません：{path}")
    doc = DocxTemplate(str(path))
    doc.render(build_context(doc, release), autoescape=True)  # &・< などが XML を壊さないように
    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def write_release(release: Release) -> str:
    """Word を作って UPLOAD_DIR に保存し、相対パスを返す。途中で失敗したらファイルは残さない。"""
    data = render_release(release)
    relative = f"releases/{release.id}/release.docx"
    final = uploads.resolve_path(relative)
    final.parent.mkdir(parents=True, exist_ok=True)
    temp = final.with_name(f".{secrets.token_hex(8)}.tmp")
    try:
        temp.write_bytes(data)
        os.replace(temp, final)  # 完成してから置き換える
    finally:
        temp.unlink(missing_ok=True)
    return relative
