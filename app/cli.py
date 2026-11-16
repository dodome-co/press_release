import click
from flask import current_app
from flask.cli import with_appcontext

from .extensions import db
from .models import ROLE_ADMIN, User


@click.command("create-admin")
@click.option("--email", prompt="メールアドレス")
@click.option("--password", prompt="パスワード", hide_input=True, confirmation_prompt=True)
@with_appcontext
def create_admin(email, password):
    """管理者を作る。画面からは作れないため、このコマンドだけが作成方法。"""
    email = User.normalize_email(email)
    if len(password) < current_app.config["PASSWORD_MIN_LENGTH"]:
        raise click.ClickException(
            f"パスワードは{current_app.config['PASSWORD_MIN_LENGTH']}文字以上にしてください。"
        )
    if User.query.filter_by(email=email).first():
        raise click.ClickException("このメールアドレスはすでに登録されています。")
    user = User(email=email, role=ROLE_ADMIN)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    click.echo(f"管理者を作成しました：{email}")
