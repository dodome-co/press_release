import pytest

from app import create_app
from app.extensions import db
from app.models import ROLE_ADMIN, User


@pytest.fixture()
def app():
    app = create_app("testing")
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
