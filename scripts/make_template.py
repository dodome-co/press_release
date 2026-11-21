"""Word の雛形（templates/press_release.docx）を作る。

運営者が Word で見た目を整える前の、仮の雛形。差し込み用の目印（{{ ... }}、{%p ... %}、
{%tr ... %}）は docxtpl の書き方。作り直すと、Word で手を入れた内容は消えるので注意。

使い方：uv run python scripts/make_template.py
"""
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Mm, Pt

OUT = Path(__file__).resolve().parent.parent / "templates" / "press_release.docx"
FONT = "游ゴシック"

doc = Document()
style = doc.styles["Normal"]
style.font.name = FONT
style.font.size = Pt(10.5)
style.element.rPr.rFonts.set(qn("w:eastAsia"), FONT)


section = doc.sections[0]
section.page_width, section.page_height = Mm(210), Mm(297)
for side in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
    setattr(section, side, Mm(20))


def para(text="", bold=False, size=None, align=None, space_after=4):
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.bold = bold
    if size:
        run.font.size = Pt(size)
    run.font.name = FONT
    run._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    if align:
        p.alignment = align
    p.paragraph_format.space_after = Pt(space_after)
    return p


# ロゴ・発信日・社名
para("{%p if logo %}")
para("{{ logo }}")
para("{%p endif %}")
para("{{ issued_date }}", align=WD_ALIGN_PARAGRAPH.RIGHT, space_after=0)
para("{{ company_name }}", align=WD_ALIGN_PARAGRAPH.RIGHT)

para("報道関係者各位", space_after=8)
para("{{ title }}", bold=True, size=15, align=WD_ALIGN_PARAGRAPH.CENTER, space_after=8)
para("{%p if subtitle %}")
para("～{{ subtitle }}～", bold=True, size=12, align=WD_ALIGN_PARAGRAPH.CENTER, space_after=8)
para("{%p endif %}")
para("{{ summary }}", space_after=10)

para("■ 取材のご案内", bold=True, size=12, space_after=6)

para("◆ 日時・会場", bold=True)
para("日時：{{ event_at }}", space_after=0)
para("会場：{{ location }}", space_after=8)

para("{%p if speakers %}")
para("◆ 当日の対応者", bold=True)
para("{%p for s in speakers %}")
para("{{ s }}", space_after=0)
para("{%p endfor %}")
para("{%p endif %}")

para("{%p if features %}")
para("◆ 商品の特徴", bold=True)
para("{%p for f in features %}")
para("・{{ f }}", space_after=0)
para("{%p endfor %}")
para("{%p endif %}")

para("◆ 会社概要", bold=True)
para("{%p for row in company_rows %}")
para("{{ row.label }}：{{ row.value }}", space_after=0)
para("{%p endfor %}")

para("◆ 本件に関するお問い合わせ先", bold=True)
para("{{ company_name }} {{ department }}　担当：{{ contact_name }}", space_after=0)
para("TEL：{{ phone }}{% if mobile_phone %}（携帯：{{ mobile_phone }}）{% endif %}", space_after=0)
para("E-mail：{{ email }}", space_after=10)

# 写真：文書の最後（お問い合わせ先の後ろの余白）に、横並びで置く。写真がなければ、何も出さない
para("{%p if photos %}")
para("{% for ph in photos %}{{ ph }}　{% endfor %}", align=WD_ALIGN_PARAGRAPH.CENTER)
para("{%p endif %}")

# フッター
footer = doc.sections[0].footer.paragraphs[0]
footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
footer.add_run("{{ company_name }} プレスリリース")

OUT.parent.mkdir(parents=True, exist_ok=True)
doc.save(OUT)
print(f"作成しました：{OUT}")
