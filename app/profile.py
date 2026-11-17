"""会員情報（企業情報・ロゴ）の確認と変更。Word の会社概要とお問い合わせ先に使われる。"""
from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user
from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField
from wtforms.validators import DataRequired, Length, Optional, ValidationError

from . import uploads
from .auth.decorators import client_required
from .auth.forms import normalize_phone, normalize_postal_code
from .prefectures import prefecture_of
from .extensions import db
from .models import INPUT_METHOD_TEXT, ServiceProfile
from .textutil import clean_text, is_safe_http_url

bp = Blueprint("profile", __name__, url_prefix="/mypage")


class ProfileForm(FlaskForm):
    company_name = StringField("社名", validators=[DataRequired("入力してください。"), Length(max=255)])
    postal_code = StringField("郵便番号", validators=[DataRequired("入力してください。")])
    address = StringField("住所", validators=[DataRequired("入力してください。"), Length(max=500)])
    department = StringField("担当部署（任意）", validators=[Optional(), Length(max=100)])
    contact_name = StringField("担当者名", validators=[DataRequired("入力してください。"), Length(max=100)])
    phone = StringField("電話番号", validators=[DataRequired("入力してください。")])
    mobile_phone = StringField("携帯電話番号（任意）", validators=[Optional()])
    founded = StringField("設立（任意）", validators=[Optional(), Length(max=50)])
    capital = StringField("資本金（任意）", validators=[Optional(), Length(max=50)])
    employees = StringField("従業員数（任意）", validators=[Optional(), Length(max=50)])
    business_description = TextAreaField("事業内容（任意）", validators=[Optional(), Length(max=500)])
    website_url = StringField("ウェブサイト URL（任意）", validators=[Optional(), Length(max=500)])

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

    def validate_website_url(self, field):
        url = (field.data or "").strip()
        # http(s) 以外（javascript: など）や、空白・改行を含むものは載せない
        if url and not is_safe_http_url(url):
            raise ValidationError("http:// または https:// で始まる URL を、空白を含めずに入力してください。")


def _blank_to_none(value):
    value = clean_text(value)
    return value or None


@bp.route("/profile", methods=["GET", "POST"])
@client_required
def edit():
    form = ProfileForm(obj=current_user if request.method == "GET" else None)
    if form.validate_on_submit():
        u = current_user
        u.company_name = clean_text(form.company_name.data)
        u.postal_code = normalize_postal_code(form.postal_code.data)
        u.address = clean_text(form.address.data)
        u.department = _blank_to_none(form.department.data)
        u.contact_name = clean_text(form.contact_name.data)
        u.phone = normalize_phone(form.phone.data)
        u.mobile_phone = normalize_phone(form.mobile_phone.data) if form.mobile_phone.data else None
        u.founded = _blank_to_none(form.founded.data)
        u.capital = _blank_to_none(form.capital.data)
        u.employees = _blank_to_none(form.employees.data)
        u.business_description = _blank_to_none(form.business_description.data)
        u.website_url = _blank_to_none(form.website_url.data)
        db.session.commit()
        flash("会員情報を保存しました。", "success")
        return redirect(url_for("profile.edit"))
    return render_template("mypage/profile.html", form=form)


@bp.post("/profile/logo")
@client_required
def upload_logo():
    file = request.files.get("logo")
    if file is None or not file.filename:
        flash("ロゴの画像を選択してください。", "error")
        return redirect(url_for("profile.edit"))
    try:
        saved = uploads.save_image(file, f"users/{current_user.id}/logo")
    except uploads.UploadError as e:
        flash(str(e), "error")
        return redirect(url_for("profile.edit"))
    old = current_user.logo_path
    current_user.logo_path = saved.relative_path
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        uploads.delete_file(saved.relative_path)
        raise
    uploads.delete_file(old)
    flash("ロゴを保存しました。", "success")
    return redirect(url_for("profile.edit"))


@bp.post("/profile/logo/delete")
@client_required
def delete_logo():
    old = current_user.logo_path
    current_user.logo_path = None
    db.session.commit()
    uploads.delete_file(old)
    flash("ロゴを削除しました。", "success")
    return redirect(url_for("profile.edit"))


class ServiceProfileForm(FlaskForm):
    body = TextAreaField("自社のサービスや商品について、自由に書いてください")

    def validate_body(self, field):
        limit = current_app.config["SERVICE_PROFILE_MAX_LENGTH"]
        if len(clean_text(field.data)) > limit:
            raise ValidationError(f"{limit}文字以内で入力してください。")


@bp.route("/service", methods=["GET", "POST"])
@client_required
def service_profile():
    """自社サービスの紹介（F10）。管理者が、書類の要旨を書くときなどの参考にする。何度でも書き直せる。"""
    profile = current_user.service_profile
    form = ServiceProfileForm(data={"body": profile.body if profile else ""})
    if form.validate_on_submit():
        body = clean_text(form.body.data)
        if not body:
            if profile is not None:
                db.session.delete(profile)  # 空にして保存すると、入力を消す
        elif profile is None:
            db.session.add(ServiceProfile(user_id=current_user.id, input_method=INPUT_METHOD_TEXT, body=body))
        else:
            profile.body = body
        db.session.commit()
        flash("自社サービスの紹介を保存しました。" if body else "自社サービスの紹介を消しました。", "success")
        return redirect(url_for("profile.service_profile"))
    return render_template(
        "mypage/service_profile.html", form=form, limit=current_app.config["SERVICE_PROFILE_MAX_LENGTH"]
    )
