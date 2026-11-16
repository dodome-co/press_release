from urllib.parse import urljoin, urlparse

from flask import current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_user, logout_user

from ..extensions import db
from ..mail import send_mail
from ..models import PasswordResetToken, User
from . import bp
from .forms import (
    ForgotPasswordForm,
    LoginForm,
    RegisterForm,
    ResetPasswordForm,
    normalize_phone,
    normalize_postal_code,
)


def _is_safe_next(target: str | None) -> bool:
    """ログイン後の移動先が自サイト内かを確認する（外部サイトへの誘導を防ぐ）。"""
    if not target:
        return False
    host_url = urlparse(request.host_url)
    redirect_url = urlparse(urljoin(request.host_url, target))
    return redirect_url.scheme in ("http", "https") and redirect_url.netloc == host_url.netloc


@bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("main.index"))
    form = RegisterForm()
    if form.validate_on_submit():
        user = User(
            email=User.normalize_email(form.email.data),
            company_name=form.company_name.data.strip(),
            postal_code=normalize_postal_code(form.postal_code.data),
            address=form.address.data.strip(),
            contact_name=form.contact_name.data.strip(),
            phone=normalize_phone(form.phone.data),
            mobile_phone=normalize_phone(form.mobile_phone.data) if form.mobile_phone.data else None,
        )
        user.set_password(form.password.data)  # 役割は必ず client（画面から管理者は作れない）
        db.session.add(user)
        db.session.commit()
        login_user(user)
        flash("会員登録が完了しました。", "success")
        return redirect(url_for("main.index"))
    return render_template("auth/register.html", form=form)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.index"))
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=User.normalize_email(form.email.data)).first()
        if user is not None and user.check_password(form.password.data):
            login_user(user)
            next_url = request.args.get("next")
            return redirect(next_url if _is_safe_next(next_url) else url_for("main.index"))
        flash("メールアドレスまたはパスワードが違います。", "error")
    return render_template("auth/login.html", form=form)


@bp.post("/logout")
def logout():
    logout_user()
    flash("ログアウトしました。", "success")
    return redirect(url_for("main.index"))


@bp.route("/password/reset", methods=["GET", "POST"])
def forgot_password():
    form = ForgotPasswordForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=User.normalize_email(form.email.data)).first()
        if user is not None:
            minutes = current_app.config["PASSWORD_RESET_EXPIRES_MINUTES"]
            token = PasswordResetToken.issue(user, minutes)
            db.session.commit()
            link = url_for("auth.reset_password", token=token, _external=True)
            send_mail(
                user.email,
                "【プレスリリースサポート】パスワード再設定のご案内",
                f"次のリンクから、{minutes}分以内に新しいパスワードを設定してください。\n"
                f"{link}\n\nお心当たりがない場合は、このメールを破棄してください。",
            )
        # 登録の有無が分からないよう、結果にかかわらず同じ画面・同じ文言にする
        return render_template("auth/forgot_sent.html")
    return render_template("auth/forgot.html", form=form)


@bp.route("/password/reset/<token>", methods=["GET", "POST"])
def reset_password(token):
    row = PasswordResetToken.find_valid(token)
    if row is None:
        flash("このリンクは無効か、期限が切れています。もう一度やり直してください。", "error")
        return redirect(url_for("auth.forgot_password"))
    form = ResetPasswordForm()
    if form.validate_on_submit():
        from ..models import utcnow

        row.user.set_password(form.password.data)
        row.used_at = utcnow()
        db.session.commit()
        flash("パスワードを変更しました。新しいパスワードでログインしてください。", "success")
        return redirect(url_for("auth.login"))
    return render_template("auth/reset.html", form=form)
