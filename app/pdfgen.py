"""プレスリリースの PDF 出力（A4）。「よいプレスリリースの定義」の形で、書類の内容から直接作る。

- 日本語のフォント（BIZ UDPゴシック）を、PDF に埋め込む（見る人のパソコンに、フォントがなくてもよい）
- A4 1枚に収まるまで、文字の大きさを、少しずつ小さくする（それでも収まらなければ、複数ページになる。ページ数を返す）
- 空の項目（サブタイトル、対応者、特徴、写真、会社概要の各行、携帯電話番号など）は、見出しや行ごと出さない
"""
from __future__ import annotations

import io
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from flask import current_app
from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import uploads
from .models import Release, utcnow
from .textutil import clean_text

FONT_DIR = Path(__file__).resolve().parent / "fonts"
REGULAR, BOLD = "BIZUDPGothic", "BIZUDPGothic-Bold"
# 文字の大きさの倍率。1枚に収まるまで、順に小さくする
SCALES = (1.0, 0.95, 0.9, 0.85, 0.8, 0.75, 0.7)
_fonts_ready = False


def _register_fonts() -> None:
    global _fonts_ready
    if _fonts_ready:
        return
    pdfmetrics.registerFont(TTFont(REGULAR, str(FONT_DIR / "BIZUDPGothic-Regular.ttf")))
    pdfmetrics.registerFont(TTFont(BOLD, str(FONT_DIR / "BIZUDPGothic-Bold.ttf")))
    pdfmetrics.registerFontFamily(REGULAR, normal=REGULAR, bold=BOLD, italic=REGULAR, boldItalic=BOLD)
    _fonts_ready = True


@dataclass
class Rendered:
    data: bytes
    pages: int  # 1 なら、A4 1枚に収まっている


def _text(value) -> str:
    """Paragraph に渡す文字列。&・< などを避け、改行を <br/> にする。"""
    return escape(clean_text(value)).replace("\n", "<br/>")  # 制御文字は、取り除く（clean_text は改行を \n にそろえる）


def _format_date(dt) -> str:
    return f"{dt.year}年{dt.month}月{dt.day}日" if dt else ""


def _image(relative_path, max_w, max_h):
    """画像を、縦横の比を保って、枠（max_w × max_h）に収めて返す。読めなければ None。"""
    if not relative_path:
        return None
    try:
        path = uploads.resolve_path(relative_path)
        if not path.is_file():
            return None
        with PILImage.open(path) as im:
            w, h = im.size
    except Exception:  # 壊れた画像・消えた画像は、飛ばす（PDF は、作れるようにする）
        return None
    ratio = min(max_w / w, max_h / h)
    return Image(str(path), width=w * ratio, height=h * ratio)


def _story(release: Release, scale: float):
    """PDF の中身（Paragraph などの並び）。scale は、文字の大きさの倍率。"""
    user = release.user
    s = scale

    def style(name, size, bold=False, align=None, leading=1.55, after=2, first_indent=0, color=colors.black):
        return ParagraphStyle(
            name, fontName=BOLD if bold else REGULAR, fontSize=size * s, leading=size * s * leading,
            alignment=align if align is not None else 0, spaceAfter=after * s, firstLineIndent=first_indent * s,
            wordWrap="CJK", textColor=color,
        )

    body = style("body", 10.5)
    heading = style("heading", 11, bold=True, after=2, leading=1.4)
    small = style("small", 9.5)
    issued = release.approved_at or utcnow()  # 承認前の仮出力は、その日の日付

    story = []

    # ロゴ（左上に、小さく）／発信日と社名（右）
    logo = _image(user.logo_path, 30 * mm * s, 14 * mm * s)
    right = [Paragraph(_text(_format_date(issued)), style("date", 10.5, align=TA_RIGHT, after=0)),
             Paragraph(_text(user.company_name), style("company", 10.5, align=TA_RIGHT, after=0))]
    top = Table([[logo or "", right]], colWidths=[50 * mm, None])
    top.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                             ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0),
                             ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    story += [top, Spacer(1, 5 * mm * s), Paragraph("報道関係者各位", style("to", 10.5, after=5))]

    # タイトル・サブタイトル・要旨
    if release.title:
        story.append(Paragraph(_text(release.title), style("title", 17, bold=True, align=TA_CENTER, leading=1.35, after=3)))
    if release.subtitle:
        story.append(Paragraph(_text(f"～{release.subtitle}～"), style("subtitle", 11.5, bold=True, align=TA_CENTER, after=7)))
    if release.summary:
        story.append(Paragraph(_text(release.summary), style("summary", 10.5, first_indent=10.5, after=8)))

    # 取材のご案内（「■」は付けず、真ん中に寄せる）
    story.append(Paragraph("取材のご案内", style("guide", 12.5, bold=True, align=TA_CENTER, after=5)))

    story += [Paragraph("◆ 日時・会場", heading),
              Paragraph(_text(f"日時：{release.event_at_display}"), style("when", 10.5, after=0)),
              Paragraph(_text(f"会場：{release.location}"), style("where", 10.5, after=6))]

    speakers = release.speaker_list
    if speakers:
        story.append(Paragraph("◆ 当日の対応者", heading))
        for i, line in enumerate(speakers):
            story.append(Paragraph(_text(line), style(f"sp{i}", 10.5, after=6 if i == len(speakers) - 1 else 0)))

    # 商品の特徴（左）と、写真（右。商品の写真は、特徴のそばに）
    photos = [_image(p.file_path, 58 * mm * s, 36 * mm * s) for p in release.photos]
    photos = [p for p in photos if p is not None]
    features = release.features
    feature_cells = []
    if features:
        feature_cells.append(Paragraph(_text(f"◆ {release.features_heading_text}"), heading))
        feature_cells += [Paragraph(_text(f"・{f}"), style(f"f{i}", 10.5, after=1)) for i, f in enumerate(features)]
    if photos:
        photo_cells = []
        for p in photos:
            p.hAlign = "RIGHT"
            photo_cells += [p, Spacer(1, 2 * mm * s)]
        table = Table([[feature_cells or "", photo_cells]], colWidths=[None, 62 * mm * s])
        table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                   ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0),
                                   ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
        story += [table, Spacer(1, 4 * mm * s)]
    elif feature_cells:
        story += feature_cells + [Spacer(1, 4 * mm * s)]

    # 会社概要（空の項目は、行ごと出さない）
    rows = [(label, value) for label, value in (
        ("社名", user.company_name), ("設立", user.founded), ("資本金", user.capital), ("従業員数", user.employees),
        ("事業内容", user.business_description), ("所在地", f"〒{user.postal_code} {user.address}"),
        ("URL", user.website_url)) if value]
    if rows:
        story.append(Paragraph("◆ 会社概要", heading))
        for i, (label, value) in enumerate(rows):
            story.append(Paragraph(_text(f"{label}：{value}"), style(f"c{i}", 10, after=6 if i == len(rows) - 1 else 0)))

    # お問い合わせ先（右端に、枠で囲んで）
    contact = [Paragraph("◆ 本件に関するお問い合わせ先", style("ch", 10.5, bold=True, after=2)),
               Paragraph(_text(f"{user.company_name} {user.department or ''}　担当：{user.contact_name}".replace("  ", " ")),
                         style("c1", 10, after=0)),
               Paragraph(_text(f"TEL：{user.phone}" + (f"（携帯：{user.mobile_phone}）" if user.mobile_phone else "")),
                         style("c2", 10, after=0)),
               Paragraph(_text(f"E-mail：{user.email}"), style("c3", 10, after=0))]
    box = Table([[contact]], colWidths=[105 * mm])
    box.hAlign = "RIGHT"
    box.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#566900")),
                             ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm), ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm),
                             ("TOPPADDING", (0, 0), (-1, -1), 2.5 * mm), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5 * mm)]))
    story.append(KeepTogether([box]))
    return story


def _build(release: Release, scale: float) -> Rendered:
    buffer = io.BytesIO()
    company = release.user.company_name

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(REGULAR, 8)
        canvas.setFillColor(colors.HexColor("#6b6355"))
        canvas.drawCentredString(A4[0] / 2, 9 * mm, f"{company} プレスリリース")
        canvas.restoreState()

    doc = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=14 * mm, bottomMargin=16 * mm,
        title=release.title or "プレスリリース", author=company,
    )
    doc.build(_story(release, scale), onFirstPage=footer, onLaterPages=footer)
    return Rendered(data=buffer.getvalue(), pages=doc.page)


def render_release(release: Release) -> Rendered:
    """PDF を作って返す（保存はしない）。A4 1枚に収まる、いちばん大きな文字の大きさを選ぶ。"""
    _register_fonts()
    result = None
    for scale in SCALES:
        result = _build(release, scale)
        if result.pages == 1:
            break
    return result


def write_release(release: Release) -> str:
    """PDF を作って UPLOAD_DIR に保存し、相対パスを返す。途中で失敗したらファイルは残さない。"""
    rendered = render_release(release)
    relative = f"releases/{release.id}/release.pdf"
    final = uploads.resolve_path(relative)
    final.parent.mkdir(parents=True, exist_ok=True)
    temp = final.with_name(f".{secrets.token_hex(8)}.tmp")
    try:
        temp.write_bytes(rendered.data)
        os.replace(temp, final)  # 完成してから置き換える
    finally:
        temp.unlink(missing_ok=True)
    return relative
