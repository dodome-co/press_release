"""入力文字列の掃除。

Word（XML）に入れられない制御文字が1つでもあると、docxtpl が書類全体の `&` `<` `>` を
黙って消してしまう。そのため、保存するときに取り除く（Word に出す直前にも、念のため掃除する）。
"""
import re

# XML 1.0 で使えない文字（改行 \n とタブ \t は使える）
_INVALID_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿\x7f]")


def clean_text(value: str | None) -> str:
    """制御文字を取り除き、改行を \\n にそろえ、前後の空白を取る。"""
    text = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    return _INVALID_XML.sub("", text).strip()
