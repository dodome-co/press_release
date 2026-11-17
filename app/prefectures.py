"""会員情報の住所から、都道府県を取り出す。

住所は「都道府県から番地・建物名まで」の形で入力してもらう（会員情報の入力欄の想定）。
先頭が都道府県の名前で始まっているときだけ、その名前を返す。それ以外は None
（画面には、住所の文字そのものは出さない。固定の名前の一覧にあるものだけを出す）。
"""
import re
import unicodedata

PREFECTURES = (
    "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県",
    "茨城県", "栃木県", "群馬県", "埼玉県", "千葉県", "東京都", "神奈川県",
    "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県", "岐阜県", "静岡県", "愛知県",
    "三重県", "滋賀県", "京都府", "大阪府", "兵庫県", "奈良県", "和歌山県",
    "鳥取県", "島根県", "岡山県", "広島県", "山口県",
    "徳島県", "香川県", "愛媛県", "高知県",
    "福岡県", "佐賀県", "長崎県", "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
)

# 住所の先頭に、郵便番号（〒123-4567）が付いていても、読み飛ばす
_LEADING_POSTAL = re.compile(r"^〒?\s*\d{3}[-ー−]?\d{4}\s*")


def prefecture_of(address: str | None) -> str | None:
    text = unicodedata.normalize("NFKC", address or "").strip()
    text = _LEADING_POSTAL.sub("", text)
    for name in PREFECTURES:
        if text.startswith(name):
            return name
    return None
