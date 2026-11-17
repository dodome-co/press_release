"""データベースのモデル。設計は docs/データベース設計.md を参照。"""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db


def utcnow() -> datetime:
    """UTC の現在時刻（タイムゾーン情報なし）。SQLite と MySQL で同じに扱うため。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


ROLE_CLIENT = "client"
ROLE_ADMIN = "admin"


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), nullable=False, unique=True)
    password_hash = db.Column(db.String(255), nullable=False)
    # セッションの世代。パスワード変更時に変え、それまでのログイン状態を無効にする
    session_nonce = db.Column(db.String(32), nullable=False, default=lambda: secrets.token_hex(16))
    role = db.Column(db.String(20), nullable=False, default=ROLE_CLIENT)
    company_name = db.Column(db.String(255), nullable=False, default="")
    postal_code = db.Column(db.String(8), nullable=False, default="")
    address = db.Column(db.String(500), nullable=False, default="")
    contact_name = db.Column(db.String(100), nullable=False, default="")
    phone = db.Column(db.String(20), nullable=False, default="")
    mobile_phone = db.Column(db.String(20), nullable=True)
    logo_path = db.Column(db.String(500), nullable=True)
    department = db.Column(db.String(100), nullable=True)  # 担当部署
    founded = db.Column(db.String(50), nullable=True)  # 設立
    capital = db.Column(db.String(50), nullable=True)  # 資本金
    employees = db.Column(db.String(50), nullable=True)  # 従業員数
    business_description = db.Column(db.Text, nullable=True)  # 事業内容
    website_url = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    reset_tokens = db.relationship(
        "PasswordResetToken", back_populates="user", cascade="all, delete-orphan"
    )
    releases = db.relationship(
        "Release",
        back_populates="user",
        foreign_keys="Release.user_id",
        cascade="all, delete-orphan",
    )
    # 注文は、ユーザーを削除しても残す（user_id が空になる）。ORM でも、消さずに切り離す
    orders = db.relationship("Order", back_populates="user", passive_deletes=False)

    @staticmethod
    def normalize_email(email: str) -> str:
        return email.strip().lower()

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)
        self.session_nonce = secrets.token_hex(16)  # 変更前のセッションをすべて無効にする

    # ---- Flask-Login 用：セッションには「id:世代」を入れる ----
    def get_id(self) -> str:
        return f"{self.id}:{self.session_nonce}"

    @classmethod
    def load_from_session_id(cls, session_id: str) -> "User | None":
        user_id, _, nonce = (session_id or "").partition(":")
        if not user_id.isdigit() or not nonce:
            return None
        user = db.session.get(cls, int(user_id))
        if user is None or not secrets.compare_digest(user.session_nonce, nonce):
            return None
        return user

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN


class PasswordResetToken(db.Model):
    __tablename__ = "password_reset_tokens"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash = db.Column(db.String(64), nullable=False, unique=True)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    user = db.relationship("User", back_populates="reset_tokens")

    @staticmethod
    def hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @classmethod
    def issue(cls, user: "User", expires_minutes: int) -> str:
        """新しい再設定用トークンを発行し、元の値（メールで送る分）を返す。

        DB にはハッシュだけを保存する。古い未使用のトークンは無効にする。
        """
        now = utcnow()
        for old in cls.query.filter_by(user_id=user.id, used_at=None).all():
            old.used_at = now
        token = secrets.token_urlsafe(32)
        db.session.add(
            cls(
                user_id=user.id,
                token_hash=cls.hash_token(token),
                expires_at=now + timedelta(minutes=expires_minutes),
            )
        )
        return token

    @classmethod
    def find_valid(cls, token: str) -> "PasswordResetToken | None":
        row = cls.query.filter_by(token_hash=cls.hash_token(token)).first()
        if row is None or row.used_at is not None or row.expires_at < utcnow():
            return None
        return row


# ---- 書類（プレスリリース）----
RELEASE_DRAFT = "draft"
RELEASE_PENDING = "pending"
RELEASE_APPROVED = "approved"
RELEASE_REJECTED = "rejected"
RELEASE_STATUSES = (RELEASE_DRAFT, RELEASE_PENDING, RELEASE_APPROVED, RELEASE_REJECTED)

SUMMARY_MANUAL = "manual"
SUMMARY_AI_ASSISTED = "ai_assisted"  # 第2弾

MAX_PHOTOS = 2
MAX_SCHEDULE_ITEMS = 5


class Release(db.Model):
    __tablename__ = "releases"
    __table_args__ = (db.Index("ix_releases_status_submitted_at", "status", "submitted_at"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title = db.Column(db.String(200), nullable=False, default="")
    event_at_text = db.Column(db.String(200), nullable=False, default="")  # 取材会の日時（自由な文字列）
    location = db.Column(db.String(300), nullable=False, default="")
    service_description = db.Column(db.Text, nullable=False, default="")
    feature_1 = db.Column(db.String(200), nullable=False, default="")
    feature_2 = db.Column(db.String(200), nullable=False, default="")
    feature_3 = db.Column(db.String(200), nullable=False, default="")
    application_method = db.Column(db.Text, nullable=False, default="")
    application_deadline = db.Column(db.String(100), nullable=False, default="")
    # 要旨の元になる情報（クライアントが入力）
    source_what = db.Column(db.Text, nullable=False, default="")
    source_why = db.Column(db.Text, nullable=False, default="")
    source_message = db.Column(db.Text, nullable=False, default="")
    # 要旨（前文）：管理者が作成・確定する
    summary = db.Column(db.Text, nullable=True)
    summary_source = db.Column(db.String(20), nullable=False, default=SUMMARY_MANUAL)

    status = db.Column(db.String(20), nullable=False, default=RELEASE_DRAFT, index=True)
    first_submitted_at = db.Column(db.DateTime, nullable=True)  # 無料枠の数え方に使う（変えない）
    submitted_at = db.Column(db.DateTime, nullable=True)
    reject_reason = db.Column(db.Text, nullable=True)
    approved_by = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    approved_at = db.Column(db.DateTime, nullable=True)
    last_edited_by = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    last_edited_at = db.Column(db.DateTime, nullable=True)
    file_path = db.Column(db.String(500), nullable=True)  # 承認時に出力した Word
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    user = db.relationship("User", back_populates="releases", foreign_keys=[user_id])
    photos = db.relationship(
        "ReleasePhoto",
        back_populates="release",
        cascade="all, delete-orphan",
        order_by="ReleasePhoto.position",
    )
    schedule_items = db.relationship(
        "ReleaseScheduleItem",
        back_populates="release",
        cascade="all, delete-orphan",
        order_by="ReleaseScheduleItem.position",
    )
    edits = db.relationship(
        "ReleaseEdit",
        back_populates="release",
        cascade="all, delete-orphan",
        order_by="ReleaseEdit.id",
    )

    @property
    def features(self) -> list[str]:
        """入力された特徴（空でないものだけ）。"""
        return [f for f in (self.feature_1, self.feature_2, self.feature_3) if f]


class ReleasePhoto(db.Model):
    __tablename__ = "release_photos"
    __table_args__ = (
        db.UniqueConstraint("release_id", "position", name="uq_release_photos_release_position"),
        db.CheckConstraint("position IN (1, 2)", name="ck_release_photos_position"),
    )

    id = db.Column(db.Integer, primary_key=True)
    release_id = db.Column(
        db.Integer, db.ForeignKey("releases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position = db.Column(db.Integer, nullable=False)
    file_path = db.Column(db.String(500), nullable=False)
    original_filename = db.Column(db.String(255), nullable=False)
    content_type = db.Column(db.String(50), nullable=False)
    size_bytes = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    release = db.relationship("Release", back_populates="photos")


class ReleaseScheduleItem(db.Model):
    __tablename__ = "release_schedule_items"
    __table_args__ = (
        db.UniqueConstraint("release_id", "position", name="uq_release_schedule_release_position"),
        db.CheckConstraint("position BETWEEN 1 AND 5", name="ck_release_schedule_position"),
    )

    id = db.Column(db.Integer, primary_key=True)
    release_id = db.Column(
        db.Integer, db.ForeignKey("releases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position = db.Column(db.Integer, nullable=False)
    time_text = db.Column(db.String(50), nullable=False)
    content = db.Column(db.String(200), nullable=False)

    release = db.relationship("Release", back_populates="schedule_items")


class ReleaseEdit(db.Model):
    """管理者の編集履歴（誰が・いつ・どの項目を・変更前→変更後）。"""

    __tablename__ = "release_edits"

    id = db.Column(db.Integer, primary_key=True)
    release_id = db.Column(
        db.Integer, db.ForeignKey("releases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    edited_by = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    edited_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    field_name = db.Column(db.String(50), nullable=False)
    old_value = db.Column(db.Text, nullable=True)
    new_value = db.Column(db.Text, nullable=True)

    release = db.relationship("Release", back_populates="edits")


# ---- 注文・決済 ----
ORDER_UNPAID = "unpaid"
ORDER_PAID = "paid"
ORDER_CANCELED = "canceled"
ORDER_REFUNDED = "refunded"
ORDER_PARTIALLY_REFUNDED = "partially_refunded"

PROGRESS_RECEIVED = "received"
PROGRESS_IN_PROGRESS = "in_progress"
PROGRESS_DELIVERED = "delivered"


class Order(db.Model):
    __tablename__ = "orders"
    __table_args__ = (
        db.Index("ix_orders_status_created_at", "status", "created_at"),
        # Stripe は、日本円で50円未満の決済ができない
        db.CheckConstraint("amount_yen >= 50", name="ck_orders_amount_min"),
        db.CheckConstraint(
            "refunded_amount >= 0 AND refunded_amount <= amount_yen", name="ck_orders_refund_within_amount"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    service_type = db.Column(db.String(30), nullable=False)
    service_name = db.Column(db.String(100), nullable=False)  # 申込み時のサービス名（退会後も分かるように）
    amount_yen = db.Column(db.Integer, nullable=False)  # 申込み時の金額（税込・円）
    status = db.Column(db.String(20), nullable=False, default=ORDER_UNPAID, index=True)
    progress = db.Column(db.String(20), nullable=True)
    due_date = db.Column(db.Date, nullable=True)
    refunded_amount = db.Column(db.Integer, nullable=False, default=0)
    refund_reason = db.Column(db.Text, nullable=True)
    materials_url = db.Column(db.String(500), nullable=True)  # 動画の素材の共有リンク
    delivery_file_path = db.Column(db.String(500), nullable=True)
    delivery_url = db.Column(db.String(500), nullable=True)
    free_revisions_used = db.Column(db.Integer, nullable=False, default=0)
    checkout_attempts = db.Column(db.Integer, nullable=False, default=0)  # 支払い画面を作った回数
    stripe_session_id = db.Column(db.String(255), nullable=True, unique=True)
    stripe_payment_intent_id = db.Column(db.String(255), nullable=True, unique=True)
    paid_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    user = db.relationship("User", back_populates="orders")
    checkout_sessions = db.relationship(
        "OrderCheckoutSession", back_populates="order", cascade="all, delete-orphan"
    )


class OrderCheckoutSession(db.Model):
    """注文のために作った Stripe の支払い画面（Checkout セッション）の履歴。

    支払いの通知が、この注文のために作ったセッションのものかを確認したり、
    古いセッションをまとめて無効にしたりするために使う。
    """

    __tablename__ = "order_checkout_sessions"

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(
        db.Integer, db.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stripe_session_id = db.Column(db.String(255), nullable=False, unique=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    order = db.relationship("Order", back_populates="checkout_sessions")


class StripeEvent(db.Model):
    """受け取った Webhook の記録。同じイベントを2回処理しないために使う。"""

    __tablename__ = "stripe_events"

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.String(255), nullable=False, unique=True)
    event_type = db.Column(db.String(100), nullable=False)
    received_at = db.Column(db.DateTime, nullable=False, default=utcnow)
