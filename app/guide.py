"""投げ込みのガイド（仕様書 F7）：記者クラブへの、プレスリリースの届け方の説明。"""
from flask import Blueprint, render_template
from flask_login import current_user

from .auth.decorators import client_required
from .prefectures import prefecture_of

bp = Blueprint("guide", __name__)


@bp.get("/guide")
@client_required
def index():
    # 会社の所在地（会員情報の住所の先頭）から、都道府県を自動で入れる。分からないときは、一般的な書き方にする
    prefecture = prefecture_of(current_user.address)
    return render_template(
        "guide/index.html",
        prefecture=prefecture,
        club_name=f"{prefecture}庁内の記者クラブ" if prefecture else "お住まいの都道府県庁内の記者クラブ",
        office_name=f"{prefecture}庁" if prefecture else "お住まいの都道府県庁",
    )
