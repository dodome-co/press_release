"""有料サービスの一覧（名前・説明・料金・納期の目安）。DB には置かず、ここに持つ。

料金は仮の金額（公開前に確定する。仕様書 9章）。環境変数で上書きできる。
- 料金が1つのサービス：PRICE_<サービスの種類>（例：PRICE_VIDEO_CREATION=40000）
- 地域などで料金が変わるサービス：PRICE_<サービスの種類>_<選択肢>（例：PRICE_INTERVIEW_PLANNING_IWATE=90000）
注文には申込み時点の金額を保存するので、変えても過去の注文には影響しない。
"""
import os
from dataclasses import dataclass

from flask import current_app

RELEASE_CREATION = "release_creation"
INTERVIEW_PLANNING = "interview_planning"  # 記者発表会の企画・当日の同席（企画支援と同行を、1つにしたもの）
DISTRIBUTION_PROXY = "distribution_proxy"  # 投げ込み交渉代行
ARTICLE_CREATION = "article_creation"
VIDEO_CREATION = "video_creation"

MIN_PAYMENT_YEN = 50  # Stripe は、日本円で50円未満の決済ができない
MAX_PAYMENT_YEN = 99_999_999  # Stripe の、日本円の上限


def _env_price(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


@dataclass(frozen=True)
class PriceOption:
    """料金が変わる選択肢（例：開催する地域）。"""

    key: str
    label: str
    default_price_yen: int  # 仮の金額（税込）
    env_suffix: str

    def price_yen(self, service_key: str) -> int:
        return _env_price(f"PRICE_{service_key.upper()}_{self.env_suffix}", self.default_price_yen)


@dataclass(frozen=True)
class Service:
    key: str
    name: str
    description: str
    default_price_yen: int  # 仮の金額（税込）。選択肢のあるサービスでは、使わない
    lead_time: str  # 納期の目安（仮。未決）
    needs_materials_url: bool = False
    has_delivery: bool = True  # 納期・進捗・納品がある（リリース作成の追加は「枠」の購入なので、ない）
    options: tuple[PriceOption, ...] = ()  # 選択肢（地域など）で料金が変わるときの、選択肢の一覧
    requires_interview: bool = True  # 管理者との事前面談が必要

    @property
    def price_yen(self) -> int:
        """選択肢のないサービスの料金。"""
        return _env_price(f"PRICE_{self.key.upper()}", self.default_price_yen)

    def get_option(self, option_key: str | None) -> PriceOption | None:
        return next((o for o in self.options if o.key == option_key), None)

    def price_for(self, option_key: str | None = None) -> int | None:
        """申し込む内容（選択肢）に応じた料金。選択肢が必要なのに、正しく選ばれていなければ None。"""
        if self.options:
            option = self.get_option(option_key)
            return option.price_yen(self.key) if option else None
        return self.price_yen

    def all_prices(self) -> list[int]:
        if self.options:
            return [o.price_yen(self.key) for o in self.options]
        return [self.price_yen]


_SERVICES = (
    Service(
        RELEASE_CREATION,
        "2件目以降のリリース作成（1件分）",
        "無料で作成できる1件を使い切ったあと、プレスリリースをもう1件作成・提出できる枠を追加します。",
        4_000,
        "お支払いの確認後、すぐにご利用いただけます。",
        has_delivery=False,
        requires_interview=False,
    ),
    Service(
        INTERVIEW_PLANNING,
        "記者発表会の企画・当日の同席",
        "記者発表会の企画をお手伝いし、当日は担当者が同席します。料金は、開催する地域によって異なります。",
        0,
        "お申し込み後、担当者から個別にご案内します。",
        options=(
            PriceOption("iwate", "岩手県内", 80_000, "IWATE"),
            PriceOption("miyagi_aomori_akita", "宮城県・青森県・秋田県", 90_000, "MIYAGI_AOMORI_AKITA"),
            PriceOption("fukushima_yamagata", "福島県・山形県", 100_000, "FUKUSHIMA_YAMAGATA"),
            PriceOption("other", "その他", 120_000, "OTHER"),
        ),
    ),
    Service(
        DISTRIBUTION_PROXY,
        "投げ込み交渉代行",
        "記者クラブに電話するなどし、クライアント様に代わり、投げ込みの交渉をいたします。岩手県内の場合は直接持ち込みます。",
        3_000,
        "お申し込み後、担当者から個別にご案内します。",
    ),
    Service(
        ARTICLE_CREATION,
        "自社媒体などに掲載する記事制作",
        "自社サイトなどに掲載する記事を制作します。納品はこのサイト上で行います。",
        10_000,
        "お申し込み後、担当者から個別にご案内します。",
    ),
    Service(
        VIDEO_CREATION,
        "自社媒体などに掲載する動画制作",
        "お預かりした素材（写真・動画）を編集して、動画を制作します（撮影は含みません）。納品は YouTube の限定公開リンクで行います。",
        30_000,
        "お申し込み後、担当者から個別にご案内します。",
        needs_materials_url=True,
    ),
)

SERVICES = {s.key: s for s in _SERVICES}

# サービス案内の画面に載せる注意書き（面談が必要なこと、複数のサービスや、ほかの希望の相談）
SERVICE_NOTES = (
    "リリース作成以外のサービスは、面談が必須となります。",
    "複数のサービスを申し込む場合は、ご相談ください。",
    "その他、ご希望のサービスがあれば、ご相談ください。",
)


def _env_names() -> list[str]:
    names = []
    for service in _SERVICES:
        if service.options:
            names += [f"PRICE_{service.key.upper()}_{o.env_suffix}" for o in service.options]
        else:
            names.append(f"PRICE_{service.key.upper()}")
    return names


def validate_price_overrides() -> None:
    """環境変数 PRICE_* の値を、起動時に確認する（誤った値でサービス一覧が壊れないように）。"""
    for name in _env_names():
        raw = os.environ.get(name)
        if not raw:
            continue
        if not (raw.isascii() and raw.isdigit()) or not MIN_PAYMENT_YEN <= int(raw) <= MAX_PAYMENT_YEN:
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
        "記者発表会の企画・当日の同席、投げ込み交渉代行、記事制作、動画制作は、作業の進み具合に応じて返金します。",
        (
            "「受付」（まだ作業を始めていない）：全額を返金します。",
            "「着手」（作業を始めた）：50%を返金します。",
            "「納品済み」：返金できません。",
        ),
    ),
    (
        "記者発表会の企画・当日の同席は、上の基準に加えて、当日、取材するメディアが一件もなければ、全額返金します。",
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
