import re
import unicodedata

from flask import current_app
from flask_wtf import FlaskForm
from wtforms import PasswordField, StringField
from wtforms.validators import DataRequired, Email, EqualTo, Length, Optional, ValidationError

from ..models import User
from ..prefectures import prefecture_of


def _digits(value: str) -> str:
    return re.sub(r"\D", "", unicodedata.normalize("NFKC", value or ""))


def normalize_postal_code(value: str) -> str | None:
    """数字7桁に直し、123-4567 の形にそろえる。7桁でなければ None。"""
    digits = _digits(value)
    if len(digits) != 7:
        return None
    return f"{digits[:3]}-{digits[3:]}"


def normalize_phone(value: str) -> str | None:
    """全角を半角にし、数字とハイフンだけにする。数字が10〜11桁でなければ None。"""
    text = unicodedata.normalize("NFKC", value or "").strip()
    digits = _digits(text)
    if not 10 <= len(digits) <= 11:
        return None
    return re.sub(r"[^0-9-]", "", text.replace("ー", "-"))


class PasswordLength:
    """パスワードの長さ（設定 PASSWORD_MIN_LENGTH 以上）を確認する。"""

    def __call__(self, form, field):
        minimum = current_app.config["PASSWORD_MIN_LENGTH"]
        maximum = current_app.config["PASSWORD_MAX_LENGTH"]
        length = len(field.data or "")
        if length < minimum:
            raise ValidationError(f"パスワードは{minimum}文字以上にしてください。")
        if length > maximum:
            raise ValidationError(f"パスワードは{maximum}文字以内にしてください。")


class RegisterForm(FlaskForm):
    email = StringField("メールアドレス", validators=[DataRequired("入力してください。"), Email("メールアドレスの形式で入力してください。"), Length(max=255)])
    password = PasswordField("パスワード", validators=[DataRequired("入力してください。"), PasswordLength()])
    password_confirm = PasswordField("パスワード（確認）", validators=[DataRequired("入力してください。"), EqualTo("password", "パスワードが一致しません。")])
    company_name = StringField("社名", validators=[DataRequired("入力してください。"), Length(max=255)])
    postal_code = StringField("郵便番号", validators=[DataRequired("入力してください。")])
    address = StringField("住所", validators=[DataRequired("入力してください。"), Length(max=500)])
    contact_name = StringField("担当者名", validators=[DataRequired("入力してください。"), Length(max=100)])
    phone = StringField("電話番号", validators=[DataRequired("入力してください。")])
    mobile_phone = StringField("携帯電話番号（任意）", validators=[Optional()])

    def validate_email(self, field):
        email = User.normalize_email(field.data)
        if User.query.filter_by(email=email).first():
            raise ValidationError("このメールアドレスは登録できません。別のアドレスをお試しください。")

    def validate_postal_code(self, field):
        if normalize_postal_code(field.data) is None:
            raise ValidationError("郵便番号は7桁の数字で入力してください（例：123-4567）。")

    def validate_address(self, field):
        if prefecture_of(field.data) is None:
            raise ValidationError("住所は、都道府県から入力してください（例：岩手県盛岡市内丸10-1）。")

    def validate_phone(self, field):
        if normalize_phone(field.data) is None:
            raise ValidationError("電話番号は10〜11桁の数字で入力してください。")

    def validate_mobile_phone(self, field):
        if field.data and normalize_phone(field.data) is None:
            raise ValidationError("携帯電話番号は10〜11桁の数字で入力してください。")


class LoginForm(FlaskForm):
    email = StringField("メールアドレス", validators=[DataRequired("入力してください。"), Length(max=255)])
    password = PasswordField("パスワード", validators=[DataRequired("入力してください。"), Length(max=256)])


class ForgotPasswordForm(FlaskForm):
    email = StringField("登録したメールアドレス", validators=[DataRequired("入力してください。"), Length(max=255)])


class ResetPasswordForm(FlaskForm):
    password = PasswordField("新しいパスワード", validators=[DataRequired("入力してください。"), PasswordLength()])
    password_confirm = PasswordField("新しいパスワード（確認）", validators=[DataRequired("入力してください。"), EqualTo("password", "パスワードが一致しません。")])
