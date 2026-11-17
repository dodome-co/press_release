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
    minimum = current_app.config["PASSWORD_MIN_LENGTH"]
    maximum = current_app.config["PASSWORD_MAX_LENGTH"]
    if not minimum <= len(password) <= maximum:
        raise click.ClickException(f"パスワードは{minimum}〜{maximum}文字にしてください。")
    if User.query.filter_by(email=email).first():
        raise click.ClickException("このメールアドレスはすでに登録されています。")
    user = User(email=email, role=ROLE_ADMIN)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    click.echo(f"管理者を作成しました：{email}")


@click.command("seed-demo")
@with_appcontext
def seed_demo():
    """デモ用のサンプルデータ（会員・書類・注文）を足す。デモ環境（DEMO_MODE）でだけ動く。"""
    from . import demo

    try:
        accounts = demo.seed()
    except demo.DemoError as e:
        raise click.ClickException(str(e))
    click.echo("サンプルデータを用意しました。ログインできるアカウント：")
    for a in accounts:
        click.echo(f"  {a['role']}：{a['email']}（{a['company']}）")
    click.echo(f"  パスワード（共通）：{demo.demo_password()}")


@click.command("reset-demo")
@click.option("--yes", is_flag=True, help="確認なしで実行する（管理者以外の会員と、注文・書類・メール・ファイルを、すべて消します）")
@with_appcontext
def reset_demo(yes):
    """デモのデータを、すべて消して、サンプルデータを作り直す。デモ環境（DEMO_MODE）でだけ動く。"""
    from . import demo

    if not yes:
        raise click.ClickException("管理者以外の会員と、注文・書類・メール・ファイルを、すべて消します。実行するなら --yes を付けてください。")
    try:
        counts = demo.wipe()
        accounts = demo.seed()
    except demo.DemoError as e:
        raise click.ClickException(str(e))
    click.echo("消したもの：" + "、".join(f"{k} {v}件" for k, v in counts.items()))
    click.echo("サンプルデータを作り直しました。ログインできるアカウント：")
    for a in accounts:
        click.echo(f"  {a['role']}：{a['email']}（{a['company']}）")
    click.echo(f"  パスワード（共通）：{demo.demo_password()}")
