"""入力文字列の掃除。

Word（XML）に入れられない制御文字が1つでもあると、docxtpl が書類全体の `&` `<` `>` を
黙って消してしまう。そのため、保存するときに取り除く（Word に出す直前にも、念のため掃除する）。
"""
import re
import unicodedata

# XML 1.0 で使えない文字（改行 \n とタブ \t は使える）
_INVALID_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿\x7f]")


def clean_text(value: str | None) -> str:
    """制御文字を取り除き、改行を \\n にそろえ、前後の空白を取る。"""
    text = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    return _INVALID_XML.sub("", text).strip()


def display_width(text: str | None) -> float:
    """全角を1、半角を0.5として数えた「全角換算の文字数」。

    Word のタイトルは1行に収めたいので、全角換算で長さを決める。
    """
    total = 0.0
    for ch in text or "":
        total += 1.0 if unicodedata.east_asian_width(ch) in ("W", "F") else 0.5
    return total


def is_safe_http_url(url: str | None, max_length: int = 500) -> bool:
    """http(s) で始まる、ホスト名のある URL か。空白・改行・制御文字・ユーザー情報（user:pw@）は不可。"""
    from urllib.parse import urlsplit

    if not url or url != url.strip() or len(url) > max_length:
        return False
    if re.search(r"[\s\x00-\x1f\x7f　]", url):
        return False
    try:
        parts = urlsplit(url)
        port = parts.port  # 範囲外の番号は、ここで例外になる
    except ValueError:
        return False
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return False
    if parts.username is not None or parts.password is not None:
        return False
    return port is None or 0 < port < 65536
