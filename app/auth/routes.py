from urllib.parse import urljoin, urlparse

from flask import current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_user, logout_user
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from .. import ratelimit
from ..extensions import db
from ..mail import send_mail
from ..models import PasswordResetToken, User, utcnow
from ..textutil import clean_text
from ..security import external_url
from . import bp
from .forms import (
    ForgotPasswordForm,
    LoginForm,
    RegisterForm,
    ResetPasswordForm,
    normalize_phone,
    normalize_postal_code,
)


# 未登録のメールでもパスワード検証と同じ時間がかかるようにするための、ダミーのハッシュ
_DUMMY_HASH = None


def _dummy_check(password: str) -> None:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = generate_password_hash("dummy-password-for-timing")
    check_password_hash(_DUMMY_HASH, password)


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
    if request.method == "POST" and not ratelimit.check_and_record(ratelimit.register_per_ip(), ratelimit.client_ip()):
        flash(TOO_MANY_MESSAGE, "error")
        return render_template("auth/register.html", form=form), 429
    if form.validate_on_submit():
        user = User(
            email=User.normalize_email(form.email.data),
            company_name=clean_text(form.company_name.data),
            postal_code=normalize_postal_code(form.postal_code.data),
            address=clean_text(form.address.data),
            contact_name=clean_text(form.contact_name.data),
            phone=normalize_phone(form.phone.data),
            mobile_phone=normalize_phone(form.mobile_phone.data) if form.mobile_phone.data else None,
        )
        user.set_password(form.password.data)  # 役割は必ず client（画面から管理者は作れない）
        db.session.add(user)
        try:
            db.session.commit()
        except IntegrityError:  # 同時に同じメールで登録されたとき
            db.session.rollback()
            form.email.errors.append("このメールアドレスは登録できません。別のアドレスをお試しください。")
            return render_template("auth/register.html", form=form)
        login_user(user)
        flash("会員登録が完了しました。", "success")
        return redirect(url_for("main.index"))
    return render_template("auth/register.html", form=form)


LOGIN_LIMIT_MESSAGE = "ログインの試行が多すぎます。しばらく（15分ほど）たってから、もう一度お試しください。"
TOO_MANY_MESSAGE = "操作が多すぎます。しばらくたってから、もう一度お試しください。"


ADMIN_HINT = "管理者の方は、管理者用のログイン（/admin/login）からお入りください。"


def _login(*, admin_only: bool):
    """ログイン（クライアント用と、管理者用の2つの入口）。

    - 管理者用（admin_only=True）：管理者のアカウントだけが入れる。クライアントのアカウントは、
      パスワードが合っていても入れず、エラーの文言は「違います」とだけ出す（管理者かどうかを明かさない）。
    - クライアント用：管理者のアカウントは、専用の入口を案内して、入れない。
    """
    template = "auth/admin_login.html" if admin_only else "auth/login.html"
    home = "admin.index_page" if admin_only else "main.index"
    if current_user.is_authenticated:
        return redirect(url_for("admin.index_page" if current_user.is_admin else "main.index"))
    form = LoginForm()
    if form.validate_on_submit():
        email = User.normalize_email(form.email.data)
        ip_limit, fail_limit = ratelimit.login_attempts_per_ip(), ratelimit.login_failures_per_account()
        # 同じ IP から多すぎる、または、同じメールアドレスで失敗が続いているときは、パスワードを確かめずに止める。
        # メールアドレスが登録されているかどうかで、結果が変わらないようにする（登録の有無を知られないように）
        if ratelimit.is_limited(ip_limit, ratelimit.client_ip()) or ratelimit.is_limited(fail_limit, email):
            flash(LOGIN_LIMIT_MESSAGE, "error")
            return render_template(template, form=form), 429
        ratelimit.record(ip_limit, ratelimit.client_ip())
        user = User.query.filter_by(email=email).first()
        if user is None:
            _dummy_check(form.password.data)  # 登録の有無が応答時間で分からないようにする
        if user is not None and user.check_password(form.password.data):
            if admin_only and not user.is_admin:
                ratelimit.record(fail_limit, email)  # クライアントのアカウントでは、管理者の入口から入れない
                flash("メールアドレスまたはパスワードが違います。", "error")
                return render_template(template, form=form)
            if not admin_only and user.is_admin:
                flash(ADMIN_HINT, "error")  # パスワードが合った人にだけ、案内する
                return render_template(template, form=form)
            ratelimit.clear(fail_limit, email)  # ログインできたら、失敗の記録は消す
            login_user(user)
            next_url = request.args.get("next")
            return redirect(next_url if _is_safe_next(next_url) else url_for(home))
        ratelimit.record(fail_limit, email)
        flash("メールアドレスまたはパスワードが違います。", "error")
    return render_template(template, form=form)


@bp.route("/login", methods=["GET", "POST"])
def login():
    return _login(admin_only=False)


@bp.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    """管理者の専用ログイン。"""
    return _login(admin_only=True)


@bp.post("/logout")
def logout():
    logout_user()
    flash("ログアウトしました。", "success")
    return redirect(url_for("main.index"))


@bp.route("/password/reset", methods=["GET", "POST"])
def forgot_password():
    form = ForgotPasswordForm()
    if request.method == "POST" and not ratelimit.check_and_record(
        ratelimit.reset_request_per_ip(), ratelimit.client_ip()
    ):
        flash(TOO_MANY_MESSAGE, "error")
        return render_template("auth/forgot.html", form=form), 429
    if form.validate_on_submit():
        email = User.normalize_email(form.email.data)
        user = User.query.filter_by(email=email).first()
        # 同じメールアドレス宛ての依頼が多いときは、メールを送らない（画面は、同じにする。メール爆撃を防ぐ）
        if user is not None and ratelimit.check_and_record(ratelimit.reset_request_per_email(), email):
            minutes = current_app.config["PASSWORD_RESET_EXPIRES_MINUTES"]
            token = PasswordResetToken.issue(user, minutes)
            db.session.commit()
            link = external_url("auth.reset_password", token=token)
            try:
                send_mail(
                    user.email,
                    "【プレスリリースサポート】パスワード再設定のご案内",
                    f"次のリンクから、{minutes}分以内に新しいパスワードを設定してください。\n"
                    f"{link}\n\nお心当たりがない場合は、このメールを破棄してください。",
                )
            except Exception:
                # 送れなかった場合も、画面は同じにする（登録の有無を知られないように）
                current_app.logger.exception("パスワード再設定メールの送信に失敗しました")
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
        # 同時に2回送られても1回しか成功しないよう、「未使用なら使用済みにする」を1回の更新で行う
        used = PasswordResetToken.query.filter_by(id=row.id, used_at=None).update({"used_at": utcnow()})
        if used != 1:
            db.session.rollback()
            flash("このリンクは無効か、期限が切れています。もう一度やり直してください。", "error")
            return redirect(url_for("auth.forgot_password"))
        row.user.set_password(form.password.data)  # 他の端末のログインも無効になる
        db.session.commit()
        flash("パスワードを変更しました。新しいパスワードでログインしてください。", "success")
        return redirect(url_for("auth.login"))
    return render_template("auth/reset.html", form=form)
