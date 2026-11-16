import pytest
from flask import g
from flask.testing import FlaskClient

from app import create_app
from app.extensions import db
from app.models import ROLE_ADMIN, User


class _FreshUserClient(FlaskClient):
    """リクエストごとに、キャッシュされたログイン中のユーザーを捨てるテスト用クライアント。

    テストでは app_context がテスト全体で共有され、Flask-Login がログイン中のユーザーを
    g に覚えてしまう。本番ではリクエストごとに作り直されるので、その状況に合わせる。
    """

    def open(self, *args, **kwargs):
        g.pop("_login_user", None)
        try:
            return super().open(*args, **kwargs)
        finally:
            g.pop("_login_user", None)


@pytest.fixture()
def app():
    app = create_app("testing")
    app.test_client_class = _FreshUserClient
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


def make_user(email="client@example.com", password="password123", role="client", **extra):
    values = dict(
        company_name="株式会社テスト",
        postal_code="123-4567",
        address="東京都千代田区1-1-1",
        contact_name="山田太郎",
        phone="03-1234-5678",
    )
    values.update(extra)
    user = User(email=email, role=role, **values)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture()
def client_user(app):
    return make_user()


@pytest.fixture()
def admin_user(app):
    return make_user(email="admin@example.com", role=ROLE_ADMIN)


def login(client, email, password="password123"):
    return client.post("/login", data={"email": email, "password": password}, follow_redirects=False)


def png_bytes(size=(40, 30), color=(30, 120, 200)):
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def isolated_upload_dir(app, tmp_path):
    """アップロードは、テストごとの一時フォルダに保存する（プロジェクト内を汚さない）。"""
    app.config["UPLOAD_DIR"] = str(tmp_path / "uploads")
