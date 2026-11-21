"""Word の雛形（templates/press_release.docx）を、「よいプレスリリースの定義」に合わせて直す。

運営者が Word で手を入れた内容を消さないよう、作り直さず、今の雛形の必要なところだけを直す：
- 「当日のスケジュール」「サービスの内容（概要）」「取材のお申し込み」を、なくす
- タイトルの下に、サブタイトルを足す
- 日時・会場の下に、「当日の対応者」を足す
- 「サービスの内容」の見出しを「商品の特徴」にする（特徴がなければ、見出しごと出さない）

使い方：uv run python scripts/update_template_for_definition.py
すでに直してあれば、何もしない。
"""
import copy
from pathlib import Path

from docx import Document
from docx.text.paragraph import Paragraph

PATH = Path(__file__).resolve().parent.parent / "templates" / "press_release.docx"


def main() -> None:
    doc = Document(PATH)
    body = doc.element.body
    text_of = lambda el: "".join(t.text or "" for t in el.iter() if t.tag.endswith("}t"))
    items = list(body.iterchildren())

    def find(prefix):
        return next(el for el in items if text_of(el).startswith(prefix))

    if "{{ subtitle }}" in "".join(text_of(el) for el in items):
        print("すでに直してあります")
        return

    title = find("【取材のお願い】")
    location = find("会場：")
    heading_h = find("◆ 日時・会場")
    line_p = find("会社概要") if False else find("{{ row.label }}")

    # サブタイトル（タイトルの下）。タイトルの段落をもとに、少し小さくする
    sub_start = copy.deepcopy(find("{%p if logo %}"))
    sub_start_el = copy.deepcopy(sub_start)
    for t in sub_start_el.iter():
        if t.tag.endswith("}t"):
            t.text = "{%p if subtitle %}"
    sub = copy.deepcopy(title)
    sub_p = Paragraph(sub, doc)
    for run in sub_p.runs[1:]:
        run._element.getparent().remove(run._element)
    sub_p.runs[0].text = "～{{ subtitle }}～"
    sub_p.runs[0].font.size = sub_p.runs[0].font.size and sub_p.runs[0].font.size * 0.8
    sub_end = copy.deepcopy(sub_start_el)
    for t in sub_end.iter():
        if t.tag.endswith("}t"):
            t.text = "{%p endif %}"
    anchor = title
    for el in (sub_start_el, sub, sub_end):
        anchor.addnext(el)
        anchor = el

    # 当日の対応者（会場の下）
    def clone_text(model, text):
        el = copy.deepcopy(model)
        p = Paragraph(el, doc)
        for run in p.runs[1:]:
            run._element.getparent().remove(run._element)
        p.runs[0].text = text
        return el

    marker = find("{%p endif %}")  # 論理の段落の見本（ロゴの endif）
    speakers = [
        clone_text(marker, "{%p if speakers %}"),
        clone_text(heading_h, "◆ 当日の対応者"),
        clone_text(marker, "{%p for s in speakers %}"),
        clone_text(location, "{{ s }}"),
        clone_text(marker, "{%p endfor %}"),
        clone_text(marker, "{%p endif %}"),
    ]
    anchor = location
    for el in speakers:
        anchor.addnext(el)
        anchor = el

    # 当日のスケジュール（{%p if schedule %} から {%p endif %} まで）を消す
    items = list(body.iterchildren())
    start = next(i for i, el in enumerate(items) if text_of(el) == "{%p if schedule %}")
    end = next(i for i in range(start, len(items)) if text_of(items[i]) == "{%p endif %}")
    for el in items[start : end + 1]:
        body.remove(el)

    # サービスの内容 → 商品の特徴。概要の段落を消し、特徴がなければ、見出しごと出さない
    items = list(body.iterchildren())
    head = next(el for el in items if text_of(el) == "◆ サービスの内容")
    head_p = Paragraph(head, doc)
    head_p.runs[0].text = "◆ 商品の特徴"
    for run in head_p.runs[1:]:
        run._element.getparent().remove(run._element)
    desc = next(el for el in items if text_of(el) == "{{ service_description }}")
    body.remove(desc)
    head.addprevious(clone_text(marker, "{%p if features %}"))
    for_start = next(el for el in body.iterchildren() if text_of(el) == "{%p for f in features %}")
    for_end = next(el for el in for_start.itersiblings() if text_of(el) == "{%p endfor %}")
    for_end.addnext(clone_text(marker, "{%p endif %}"))

    # 取材のお申し込み（見出し・方法・締切・注意書き）を消す
    items = list(body.iterchildren())
    start = next(i for i, el in enumerate(items) if text_of(el) == "◆ 取材のお申し込み")
    end = next(i for i, el in enumerate(items) if text_of(el).startswith("※お申し込み後"))
    for el in items[start : end + 1]:
        body.remove(el)

    doc.save(PATH)
    print("雛形を直しました：", PATH)


if __name__ == "__main__":
    main()
