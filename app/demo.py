"""デモ用のサンプルデータ。ビジネスプランの発表のために、画面に見せるデータを一括で用意する。

`flask seed-demo`（足す）と `flask reset-demo --yes`（消して、作り直す）から使う。
デモ環境（DEMO_MODE）でだけ動く。本物のお金は動かない：Stripe の支払い ID が `pi_demo_` で
始まる注文だけ、デモ環境で、返金を模擬する（app/payments.py）。
"""
import io
import os
from datetime import date, timedelta

from docx import Document
from flask import current_app
from PIL import Image, ImageDraw
from werkzeug.datastructures import FileStorage

from . import docgen, uploads
from .extensions import db
from .models import (
    MailLog,
    Order,
    OrderRefund,
    Release,
    ReleaseScheduleItem,
    ReleasePhoto,
    ROLE_ADMIN,
    ROLE_CLIENT,
    ServiceProfile,
    StripeEvent,
    User,
    utcnow,
)

DEMO_PASSWORD_DEFAULT = "demo-pass-2026"
ADMIN_EMAIL = "demo-admin@example.com"

CLIENTS = (
    {
        "email": "demo-client1@example.com",
        "company_name": "株式会社いわて菓子工房",
        "postal_code": "020-0023",
        "address": "岩手県盛岡市内丸10-1",
        "department": "広報部",
        "contact_name": "盛岡 花子",
        "phone": "019-000-1111",
        "mobile_phone": "090-0000-1111",
        "founded": "1985年4月",
        "capital": "1,000万円",
        "employees": "25名",
        "business_description": "和菓子の製造・販売、オンラインショップの運営",
        "website_url": "https://example.com/iwate-kashi",
        "logo_color": (200, 90, 60),
    },
    {
        "email": "demo-client2@example.com",
        "company_name": "株式会社みやぎ観光",
        "postal_code": "980-0021",
        "address": "宮城県仙台市青葉区中央1-1-1",
        "department": "企画課",
        "contact_name": "仙台 太郎",
        "phone": "022-000-2222",
        "mobile_phone": None,
        "founded": "2010年7月",
        "capital": "500万円",
        "employees": "8名",
        "business_description": "東北の体験型観光プランの企画・販売",
        "website_url": None,
        "logo_color": (50, 120, 180),
    },
    {
        "email": "demo-client3@example.com",
        "company_name": "合同会社あおもりデザイン",
        "postal_code": "030-0801",
        "address": "青森県青森市新町1-1-1",
        "department": None,
        "contact_name": "青森 次郎",
        "phone": "017-000-3333",
        "mobile_phone": None,
        "founded": None,
        "capital": None,
        "employees": None,
        "business_description": None,
        "website_url": None,
        "logo_color": None,
    },
)


class DemoError(Exception):
    """デモ環境でないときなど、利用者に見せてよいメッセージを持つエラー。"""


def require_demo_mode() -> None:
    if not current_app.config.get("DEMO_MODE"):
        raise DemoError("デモ環境（DEMO_MODE=1 か APP_ENV=demo）でだけ実行できます。")


def demo_password() -> str:
    return os.environ.get("DEMO_PASSWORD") or DEMO_PASSWORD_DEFAULT


# ---- 画像・ファイルの生成 ----
def _picture(size, color, label: str) -> bytes:
    image = Image.new("RGB", size, color)
    draw = ImageDraw.Draw(image)
    w, h = size
    draw.rectangle([w * 0.08, h * 0.12, w * 0.92, h * 0.88], outline=(255, 255, 255), width=max(2, w // 120))
    draw.ellipse([w * 0.40, h * 0.30, w * 0.60, h * 0.30 + w * 0.20], outline=(255, 255, 255), width=max(2, w // 120))
    draw.text((w * 0.1, h * 0.92), label, fill=(255, 255, 255))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _store_image(data: bytes, subdir: str, name: str, landscape=False):
    return uploads.save_image(FileStorage(stream=io.BytesIO(data), filename=name), subdir, require_landscape=landscape)


def _article_docx(company: str) -> bytes:
    doc = Document()
    doc.add_heading(f"{company}の新商品が、地元で話題に", level=1)
    doc.add_paragraph("（デモ用の納品物です）自社サイトに掲載する記事の本文が、ここに入ります。")
    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


# ---- 作る ----
def _make_user(spec: dict, role: str = ROLE_CLIENT) -> User:
    user = User(
        email=spec["email"], role=role, company_name=spec.get("company_name", ""),
        postal_code=spec.get("postal_code", ""), address=spec.get("address", ""),
        department=spec.get("department"), contact_name=spec.get("contact_name", ""),
        phone=spec.get("phone", ""), mobile_phone=spec.get("mobile_phone"),
        founded=spec.get("founded"), capital=spec.get("capital"), employees=spec.get("employees"),
        business_description=spec.get("business_description"), website_url=spec.get("website_url"),
    )
    user.set_password(demo_password())
    db.session.add(user)
    db.session.commit()
    if spec.get("logo_color"):
        user.logo_path = _store_image(
            _picture((300, 300), spec["logo_color"], "LOGO"), f"users/{user.id}/logo", "logo.png"
        ).relative_path
        db.session.commit()
    return user


def _release(user: User, admin: User, *, approved: bool, title: str, days_ago: int) -> Release:
    submitted = utcnow() - timedelta(days=days_ago)
    release = Release(
        user_id=user.id,
        title=title,
        event_at_text="2026年12月5日（土）14:00〜15:30",
        location=f"{user.company_name} 本店（{user.address}）",
        service_description="地元の素材を使った、新しい和菓子のシリーズを発売します。試食会で、作り手が味と製法をご説明します。",
        feature_1="地元産の栗と、岩手の米粉を使用",
        feature_2="砂糖を控えめにした、やさしい甘さ",
        feature_3="お土産にも使える、軽くて持ち運びやすい箱",
        application_method="お問い合わせ先に、メールまたは電話でお申し込みください。",
        application_deadline="12月3日（木）17:00まで",
        source_what="新シリーズの試食会を、取材してほしい",
        source_why="地元の素材を使った商品で、地域の農家と、観光の活性化につなげたい",
        source_message="伝統の味を、若い世代にも届けたい",
        status="pending",
        first_submitted_at=submitted,
        submitted_at=submitted,
    )
    db.session.add(release)
    db.session.flush()
    for position, (time_text, content) in enumerate(
        (("14:00〜14:20", "開発の背景のご説明"), ("14:20〜15:00", "試食会"), ("15:00〜15:30", "質疑応答・個別取材")), start=1
    ):
        release.schedule_items.append(ReleaseScheduleItem(position=position, time_text=time_text, content=content))
    for position, color in enumerate(((190, 120, 70), (90, 130, 90)), start=1):
        saved = _store_image(
            _picture((900, 600), color, f"PHOTO {position}"), f"releases/{release.id}/photos", f"photo{position}.png",
            landscape=True,
        )
        db.session.add(ReleasePhoto(release_id=release.id, position=position, file_path=saved.relative_path,
                                    original_filename=saved.original_filename, content_type=saved.content_type,
                                    size_bytes=saved.size_bytes))
    db.session.commit()
    if approved:
        release.summary = (
            f"{user.company_name}は、地元の素材を使った新しい和菓子のシリーズを発売するにあたり、試食会を開催します。"
            "地域の農家と連携した取り組みで、伝統の味を次の世代へつなぐ試みです。ぜひ、取材いただきますようご案内申し上げます。"
        )
        release.approved_by = admin.id
        release.approved_at = utcnow() - timedelta(days=days_ago - 1)
        release.status = "approved"
        release.file_path = docgen.write_release(release)
        db.session.commit()
    return release


def _order(user: User, service_type: str, name: str, amount: int, *, option=None, status="paid", progress="received",
           days_ago=5, **extra) -> Order:
    order = Order(
        user_id=user.id, service_type=service_type, service_name=name, service_option=option, amount_yen=amount,
        status=status, progress=progress,
        paid_at=utcnow() - timedelta(days=days_ago) if status != "unpaid" else None,
        stripe_payment_intent_id=f"pi_demo_{service_type}_{user.id}" if status != "unpaid" else None,
        **extra,
    )
    db.session.add(order)
    db.session.commit()
    return order


def seed() -> list[dict]:
    """サンプルデータを足す。すでにある会員は、そのままにする。作った（または、あった）アカウントの一覧を返す。"""
    require_demo_mode()
    accounts = []
    admin = User.query.filter_by(email=ADMIN_EMAIL).first()
    if admin is None:
        admin = _make_user({"email": ADMIN_EMAIL}, role=ROLE_ADMIN)
    accounts.append({"role": "管理者", "email": ADMIN_EMAIL, "company": "運営"})

    existing = {u.email for u in User.query.filter(User.email.in_([c["email"] for c in CLIENTS])).all()}
    for spec in CLIENTS:
        accounts.append({"role": "クライアント", "email": spec["email"], "company": spec["company_name"]})
        if spec["email"] in existing:
            continue
        user = _make_user(spec)
        if spec["email"] == "demo-client1@example.com":
            _seed_client1(user, admin)
        elif spec["email"] == "demo-client2@example.com":
            _seed_client2(user, admin)
        else:
            _seed_client3(user)
    return accounts


def _seed_client1(user: User, admin: User) -> None:
    """承認済みの書類と、いろいろな状態の注文（納品済み・進行中・一部返金）がある会員。"""
    _release(user, admin, approved=True, title="新作和菓子「いわて栗ようかん」試食会", days_ago=20)
    db.session.add(ServiceProfile(user_id=user.id, body=(
        "創業40年の和菓子店です。岩手の素材（栗・米・りんご）にこだわり、地元の農家と一緒に商品を作っています。"
        "来年は、県外のお客様にも、オンラインで届けたいと考えています。")))
    db.session.commit()
    _order(user, "distribution_proxy", "投げ込み交渉代行", 3000, progress="delivered", days_ago=12,
           delivered_at=utcnow() - timedelta(days=10))
    article = _order(user, "article_creation", "自社媒体などに掲載する記事制作", 10000, progress="delivered", days_ago=9,
                     delivered_at=utcnow() - timedelta(days=6))
    saved = uploads.save_document(
        FileStorage(stream=io.BytesIO(_article_docx(user.company_name)), filename="記事.docx"), f"orders/{article.id}/delivery"
    )
    article.delivery_file_path, article.delivery_filename = saved.relative_path, "記事.docx"
    _order(user, "interview_planning", "記者発表会の企画・当日の同席（岩手県内）", 80000, option="iwate",
           progress="in_progress", days_ago=4, due_date=date.today() + timedelta(days=14))
    video = _order(user, "video_creation", "自社媒体などに掲載する動画制作", 30000, progress="in_progress", days_ago=3,
                   materials_url="https://drive.example.com/iwate-kashi/materials",
                   status="partially_refunded", refunded_amount=15000)
    db.session.add(OrderRefund(order_id=video.id, amount_yen=15000, reason="policy", status="succeeded",
                               stripe_refund_id="re_demo_seed", note="素材の差し替えのため、着手前の分を返金"))
    db.session.commit()


def _seed_client2(user: User, admin: User) -> None:
    """承認待ちの書類がある会員（承認の画面を見せるため）。"""
    _release(user, admin, approved=False, title="体験型観光「ものづくり旅」説明会", days_ago=1)
    db.session.add(ServiceProfile(user_id=user.id, body="東北の職人さんの工房を訪ねる、体験型の観光プランを企画・販売しています。"))
    _order(user, "release_creation", "2件目以降のリリース作成（1件分）", 4000, progress=None, days_ago=2)
    db.session.commit()


def _seed_client3(user: User) -> None:
    """何もない、新規の会員（会員登録から、書類の作成までを見せるため）。下書きだけ、1件。"""
    db.session.add(Release(user_id=user.id, title="（下書き）新しいサービスのお知らせ"))
    db.session.commit()


# ---- 消す ----
def wipe() -> dict:
    """管理者以外の会員と、注文・書類・メール・ファイルを、すべて消す（デモ環境でだけ）。"""
    require_demo_mode()
    clients = User.query.filter(User.role != ROLE_ADMIN).all()
    counts = {"会員": len(clients), "注文": Order.query.count(), "書類": Release.query.count(),
              "メール": MailLog.query.count()}
    for order in Order.query.all():
        db.session.delete(order)
    for user in clients:
        db.session.delete(user)  # 書類・写真・パスワード再設定・自社サービスの紹介も、一緒に消える
    MailLog.query.delete()
    StripeEvent.query.delete()
    db.session.commit()
    for directory in ("releases", "users", "orders"):
        uploads.delete_directory(directory)
    return counts
