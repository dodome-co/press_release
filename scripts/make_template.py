"""Word の雛形（templates/press_release.docx）を作る。

運営者が Word で見た目を整える前の、仮の雛形。差し込み用の目印（{{ ... }}、{%p ... %}、
{%tr ... %}）は docxtpl の書き方。作り直すと、Word で手を入れた内容は消えるので注意。

使い方：uv run python scripts/make_template.py
"""
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Pt

OUT = Path(__file__).resolve().parent.parent / "templates" / "press_release.docx"
FONT = "游ゴシック"

doc = Document()
style = doc.styles["Normal"]
style.font.name = FONT
style.font.size = Pt(10.5)
style.element.rPr.rFonts.set(qn("w:eastAsia"), FONT)


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
para("【取材のお願い】{{ title }}", bold=True, size=16, space_after=8)
para("{{ summary }}", space_after=10)

para("■ 取材のご案内", bold=True, size=12, space_after=6)

para("◆ 日時・会場", bold=True)
para("日時：{{ event_at }}", space_after=0)
para("会場：{{ location }}", space_after=8)

para("{%p if schedule %}")
para("◆ 当日のスケジュール", bold=True)
table = doc.add_table(rows=3, cols=2)
table.style = "Table Grid"
table.rows[0].cells[0].text = "時間"
table.rows[0].cells[1].text = "内容"
for cell in table.rows[0].cells:
    for run in cell.paragraphs[0].runs:
        run.bold = True
table.rows[1].cells[0].text = "{%tr for item in schedule %}"
table.rows[1].cells[1].text = ""
# docxtpl の行の繰り返し：開始の行、中身の行、終了の行
table.rows[2].cells[0].text = "{{ item.time }}"
table.rows[2].cells[1].text = "{{ item.content }}"
end_row = table.add_row()
end_row.cells[0].text = "{%tr endfor %}"
end_row.cells[1].text = ""
para("{%p endif %}")

para("◆ サービスの内容", bold=True)
para("{{ service_description }}")
para("{%p for f in features %}")
para("・{{ f }}", space_after=0)
para("{%p endfor %}")

para("{%p if photos %}")
para("◆ 写真", bold=True)
para("{%p for ph in photos %}")
para("{{ ph }}")
para("{%p endfor %}")
para("{%p endif %}")

para("◆ 取材のお申し込み", bold=True)
para("方法：{{ application_method }}", space_after=0)
para("締切：{{ application_deadline }}", space_after=4)
para("※お申し込み後にご都合が悪くなり、取材にお越しいただけなくなった場合は、お手数ですが下記のお問い合わせ先までご連絡ください。", space_after=8)

para("◆ 会社概要", bold=True)
para("{%p for row in company_rows %}")
para("{{ row.label }}：{{ row.value }}", space_after=0)
para("{%p endfor %}")

para("◆ 本件に関するお問い合わせ先", bold=True)
para("{{ company_name }} {{ department }}　担当：{{ contact_name }}", space_after=0)
para("TEL：{{ phone }}{% if mobile_phone %}（携帯：{{ mobile_phone }}）{% endif %}", space_after=0)
para("E-mail：{{ email }}", space_after=10)

# フッター
footer = doc.sections[0].footer.paragraphs[0]
footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
footer.add_run("{{ company_name }} プレスリリース")

OUT.parent.mkdir(parents=True, exist_ok=True)
doc.save(OUT)
print(f"作成しました：{OUT}")
