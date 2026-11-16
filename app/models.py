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
    role = db.Column(db.String(20), nullable=False, default=ROLE_CLIENT)
    company_name = db.Column(db.String(255), nullable=False, default="")
    postal_code = db.Column(db.String(8), nullable=False, default="")
    address = db.Column(db.String(500), nullable=False, default="")
    contact_name = db.Column(db.String(100), nullable=False, default="")
    phone = db.Column(db.String(20), nullable=False, default="")
    mobile_phone = db.Column(db.String(20), nullable=True)
    logo_path = db.Column(db.String(500), nullable=True)
    company_profile = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    reset_tokens = db.relationship(
        "PasswordResetToken", back_populates="user", cascade="all, delete-orphan"
    )

    @staticmethod
    def normalize_email(email: str) -> str:
        return email.strip().lower()

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

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
