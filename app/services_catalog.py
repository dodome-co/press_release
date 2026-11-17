"""有料サービスの一覧（名前・説明・料金・納期の目安）。DB には置かず、ここに持つ。

料金は仮の金額（公開前に確定する。仕様書 9章）。環境変数 PRICE_<サービスの種類> で上書きできる
（例：PRICE_VIDEO_CREATION=60000）。注文には申込み時点の金額を保存するので、変えても過去の注文には影響しない。
"""
import os
from dataclasses import dataclass

from flask import current_app

RELEASE_CREATION = "release_creation"
INTERVIEW_PLANNING = "interview_planning"
INTERVIEW_ACCOMPANY = "interview_accompany"
DISTRIBUTION_PROXY = "distribution_proxy"
ARTICLE_CREATION = "article_creation"
VIDEO_CREATION = "video_creation"

MIN_PAYMENT_YEN = 50  # Stripe は、日本円で50円未満の決済ができない


@dataclass(frozen=True)
class Service:
    key: str
    name: str
    description: str
    default_price_yen: int  # 仮の金額（税込）
    lead_time: str  # 納期の目安（仮。未決）
    needs_materials_url: bool = False
    has_delivery: bool = True  # 納期・進捗・納品がある（リリース作成の追加は「枠」の購入なので、ない）

    @property
    def price_yen(self) -> int:
        env = os.environ.get(f"PRICE_{self.key.upper()}")
        return int(env) if env else self.default_price_yen


_SERVICES = (
    Service(
        RELEASE_CREATION,
        "2件目以降のリリース作成（1件分）",
        "無料で作成できる1件を使い切ったあと、プレスリリースをもう1件作成・提出できる枠を追加します。",
        5_000,
        "お支払いの確認後、すぐにご利用いただけます。",
        has_delivery=False,
    ),
    Service(
        INTERVIEW_PLANNING,
        "取材会の企画支援",
        "取材会の目的や進め方、当日のスケジュールの組み立てをお手伝いします。当日、取材するメディアが一件もなければ、全額返金します。",
        60_000,
        "お申し込み後、担当者から個別にご案内します。",
    ),
    Service(
        INTERVIEW_ACCOMPANY,
        "取材会の同行",
        "取材会の当日に、担当者が同行してサポートします。",
        50_000,
        "お申し込み後、担当者から個別にご案内します。",
    ),
    Service(
        DISTRIBUTION_PROXY,
        "投げ込み代行",
        "完成したプレスリリースを、報道関係者へ届ける作業を代行します。",
        10_000,
        "お申し込み後、担当者から個別にご案内します。",
    ),
    Service(
        ARTICLE_CREATION,
        "自社記事などに掲載する記事作成",
        "自社サイトなどに掲載する記事を作成します。納品はこのサイト上で行います。",
        20_000,
        "お申し込み後、担当者から個別にご案内します。",
    ),
    Service(
        VIDEO_CREATION,
        "自社記事などに掲載する動画作成",
        "お預かりした素材（写真・動画）を編集して、動画を作成します（撮影は含みません）。納品は YouTube の限定公開リンクで行います。",
        50_000,
        "お申し込み後、担当者から個別にご案内します。",
        needs_materials_url=True,
    ),
)

SERVICES = {s.key: s for s in _SERVICES}


MAX_PAYMENT_YEN = 99_999_999  # Stripe の、日本円の上限


def validate_price_overrides() -> None:
    """環境変数 PRICE_* の値を、起動時に確認する（誤った値でサービス一覧が壊れないように）。"""
    for service in _SERVICES:
        name = f"PRICE_{service.key.upper()}"
        raw = os.environ.get(name)
        if not raw:
            continue
        if not raw.isdigit() or not MIN_PAYMENT_YEN <= int(raw) <= MAX_PAYMENT_YEN:
            raise RuntimeError(
                f"{name} は、{MIN_PAYMENT_YEN:,}〜{MAX_PAYMENT_YEN:,} の整数（円・カンマなし）で設定してください：{raw!r}"
            )


def all_services() -> list[Service]:
    return list(_SERVICES)


def get_service(key: str) -> Service | None:
    return SERVICES.get(key)


# キャンセル・返金の基準（仕様書 9章）。申込み前の画面と、注文確認メールの両方で、同じ内容を示す。
# 各項目は (見出しの文, その下の箇条書き) 。
REFUND_POLICY = (
    (
        "取材会の企画支援・取材会の同行・投げ込み代行・記事作成・動画作成は、作業の進み具合に応じて返金します。",
        (
            "「受付」（まだ作業を始めていない）：全額を返金します。",
            "「着手」（作業を始めた）：50%を返金します。",
            "「納品済み」：返金できません。",
        ),
    ),
    (
        "取材会の企画支援は、上の基準に加えて、当日、取材するメディアが一件もなければ、全額返金します。",
        (),
    ),
    (
        "2件目以降のリリース作成は、まだ使っていない分は全額を返金します。すでに書類の提出に使った分は、返金できません。",
        (),
    ),
    (
        "キャンセルをご希望の場合は、お問い合わせ先までご連絡ください。",
        (),
    ),
)


def refund_policy_text() -> str:
    """キャンセルと返金の基準を、メール用の文章にする。"""
    lines = ["【キャンセル・返金について】"]
    for heading, items in REFUND_POLICY:
        lines.append(f"・{heading}")
        lines.extend(f"　- {item}" for item in items)
    return "\n".join(lines)


def support_contact() -> str:
    return current_app.config.get("SUPPORT_EMAIL") or "（準備中）"
