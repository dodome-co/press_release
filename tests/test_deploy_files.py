"""VM への設置の、設定ファイルと手順書の確認（実際の Nginx・systemd は、この環境にないため、中身を確認する）。"""
import ast
import re
from pathlib import Path

import pytest

DEPLOY = Path("deploy")


def _read(name: str) -> str:
    return (DEPLOY / name).read_text(encoding="utf-8")


# ---- gunicorn ----
def test_gunicorn_conf_is_valid_python_and_listens_only_locally():
    source = _read("gunicorn.conf.py")
    ast.parse(source)
    assert '"127.0.0.1:8000"' in source  # 外には出さない（Nginx の後ろだけ）
    assert "0.0.0.0" not in source


def test_gunicorn_conf_values():
    namespace: dict = {}
    exec(compile(_read("gunicorn.conf.py"), "gunicorn.conf.py", "exec"), namespace)
    assert namespace["bind"] == "127.0.0.1:8000"
    assert 2 <= namespace["workers"] <= 3 and namespace["timeout"] >= 60
    assert namespace["accesslog"] == "-" and namespace["errorlog"] == "-"  # ログは systemd へ
    assert "x-forwarded-for" in namespace["access_log_format"]


# ---- systemd ----
def test_systemd_unit_runs_as_a_limited_user_with_secrets_from_a_protected_file():
    unit = _read("press-release.service")
    assert "User=pressrel" in unit and "Group=pressrel" in unit and "User=root" not in unit
    assert "EnvironmentFile=/etc/press-release.env" in unit
    assert 'ExecStart=/opt/press-release/.venv/bin/gunicorn -c deploy/gunicorn.conf.py "app:create_app()"' in unit
    assert "Restart=on-failure" in unit and "WantedBy=multi-user.target" in unit


def test_systemd_unit_is_sandboxed():
    unit = _read("press-release.service")
    for directive in ("NoNewPrivileges=true", "PrivateTmp=true", "ProtectSystem=strict", "ProtectHome=true",
                      "ReadWritePaths=/var/lib/press-release"):
        assert directive in unit, directive


def test_systemd_unit_has_no_secrets():
    unit = _read("press-release.service")
    assert not re.search(r"sk_(test|live)_|whsec_|SECRET_KEY\s*=", unit)


# ---- Nginx ----
def test_nginx_requires_a_password_except_for_the_stripe_webhook():
    conf = _read("nginx-press-release.conf")
    assert "auth_basic_user_file /etc/nginx/press-release.htpasswd;" in conf
    webhook = conf[conf.index("location = /stripe/webhook"):]
    webhook = webhook[:webhook.index("}\n", webhook.index("proxy_pass")) + 1]
    assert "auth_basic off;" in webhook and "limit_except POST { deny all; }" in webhook
    # 合言葉の対象外は、webhook だけ
    assert conf.count("auth_basic off;") == 1


def test_nginx_allows_the_biggest_upload_and_hides_dotfiles():
    conf = _read("nginx-press-release.conf")
    size = int(re.search(r"client_max_body_size (\d+)m;", conf).group(1))
    assert size >= 11  # 納品する記事（最大10MB）＋フォームの分
    assert "location ~ /\\.(?!well-known) { deny all; }" in conf
    assert "server_tokens off;" in conf and "listen 80 default_server;" in conf


def test_nginx_upload_limit_is_not_smaller_than_the_apps_limit(app):
    conf = _read("nginx-press-release.conf")
    size_mb = int(re.search(r"client_max_body_size (\d+)m;", conf).group(1))
    assert size_mb * 1024 * 1024 >= app.config["UPLOAD_MAX_DOCUMENT_BYTES"]  # Nginx が先に断らない
    assert size_mb * 1024 * 1024 <= app.config["MAX_CONTENT_LENGTH"] + 1024 * 1024  # アプリより、緩すぎない


def test_nginx_overwrites_forwarded_for_instead_of_appending():
    """相手が付けてきた X-Forwarded-For を引き継ぐと、回数制限を、IP の偽装で、すり抜けられる。"""
    snippet = re.sub(r"#.*", "", _read("nginx-proxy-snippet.conf"))  # コメントは除く（説明の文章に、変数の名前が出るため）
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in snippet
    assert "$proxy_add_x_forwarded_for" not in snippet
    assert 'proxy_set_header Authorization "";' in snippet  # 合言葉は、アプリに渡さない
    assert "proxy_set_header Host $host;" in snippet


def test_nginx_proxies_only_to_the_local_gunicorn():
    conf = _read("nginx-press-release.conf")
    assert conf.count("proxy_pass http://127.0.0.1:8000;") == 2
    assert "include /etc/nginx/snippets/press-release-proxy.conf;" in conf


def test_nginx_braces_are_balanced():
    for name in ("nginx-press-release.conf",):
        text = re.sub(r"#.*", "", _read(name))
        assert text.count("{") == text.count("}")


def test_nginx_serves_static_files_from_the_apps_static_folder(app):
    conf = _read("nginx-press-release.conf")
    assert "alias /opt/press-release/app/static/;" in conf
    assert Path(app.static_folder).name == "static" and Path("app/static/css/style.css").is_file()


# ---- 設定の例 ----
def test_env_example_has_every_setting_the_demo_needs(monkeypatch):
    from app.config import DemoConfig

    text = _read("env.demo.example")
    values = dict(re.findall(r"^([A-Z_]+)=(.*)$", text, flags=re.M))
    assert values["APP_ENV"] == "demo" and values["MAIL_BACKEND"] == "database"
    for key in ("SECRET_KEY", "BASE_URL", "TRUSTED_HOSTS", "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET",
                "DATABASE_URL", "UPLOAD_DIR"):
        assert key in values, key
    assert values["BASE_URL"].startswith("http://") and values["DATABASE_URL"].startswith("sqlite:////var/lib/press-release/")
    assert values["UPLOAD_DIR"].startswith("/var/lib/press-release/")


def test_env_example_contains_no_real_secrets():
    text = _read("env.demo.example")
    assert not re.search(r"sk_(test|live)_[A-Za-z0-9]{12,}|whsec_[A-Za-z0-9]{12,}", text)
    assert "sk_live_" not in text.replace("sk_live_ 以外", "")


def test_env_example_paths_match_the_systemd_unit_and_nginx():
    env = _read("env.demo.example")
    assert "/var/lib/press-release" in env and "/var/lib/press-release" in _read("press-release.service")
    assert "/etc/press-release.env" in _read("press-release.service")


# ---- 更新・バックアップのスクリプト ----
@pytest.mark.parametrize("name", ["update.sh", "backup.sh"])
def test_scripts_are_executable_and_strict(name):
    path = DEPLOY / name
    assert path.stat().st_mode & 0o111, f"{name} に実行の権限がない"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash") and "set -euo pipefail" in text


def test_update_script_migrates_before_restarting_and_does_not_force():
    text = _read("update.sh")
    assert "git pull --ff-only" in text and "--force" not in text and "reset --hard" not in text
    assert text.index("flask db upgrade") < text.index("systemctl restart press-release")
    assert "--frozen --no-dev" in text


def test_update_script_runs_as_root_and_does_app_steps_as_pressrel():
    """pressrel には sudo の権限がないので、スクリプト全体は sudo で動かし、取り込みと更新だけを pressrel で行う。"""
    text = _read("update.sh")
    assert "sudo /opt/press-release/deploy/update.sh" in text  # 使い方
    assert "$EUID -ne 0" in text  # sudo なしで動かしたら、最初に止める
    assert 'sudo -u "$APP_USER"' in text and "APP_USER=pressrel" in text
    assert "sudo systemctl" not in text  # 再起動は、すでに管理者の権限で動いているので、sudo を付けない
    for step in ("git pull --ff-only", "uv sync --frozen --no-dev", "flask db upgrade"):
        line = next(line for line in text.splitlines() if step in line)
        assert line.strip().startswith("as_app "), step  # アプリ用のユーザーで実行する
    guide = Path("docs/VM設置手順.md").read_text(encoding="utf-8")
    assert "sudo /opt/press-release/deploy/update.sh" in guide
    assert "sudo -u pressrel /opt/press-release/deploy/update.sh" not in guide


def test_backup_script_uses_sqlite_backup_and_keeps_seven_days():
    text = _read("backup.sh")
    assert ".backup" in text and "cp " not in text  # 動いている最中に、ファイルを、単にコピーしない
    assert "-mtime +7 -delete" in text and 'chmod 600 "$DEST"/*' in text


# ---- 手順書 ----
def test_guide_mentions_every_file_and_the_key_warnings():
    guide = Path("docs/VM設置手順.md").read_text(encoding="utf-8")
    for expected in ("deploy/env.demo.example", "deploy/press-release.service", "deploy/nginx-press-release.conf",
                     "deploy/nginx-proxy-snippet.conf", "update.sh", "backup.sh", "固定の外部 IP", "sk_live_",
                     "個人情報を、入力しない", "/stripe/webhook", "htpasswd", "flask seed-demo", "reset-demo",
                     "VM を止める", "解放"):
        assert expected in guide, expected


def test_guide_lists_all_stripe_events_the_app_handles():
    guide = Path("docs/VM設置手順.md").read_text(encoding="utf-8")
    from app.orders.service import HANDLED_PAID

    for event in (*HANDLED_PAID, "checkout.session.expired", "charge.refunded", "charge.dispute.created",
                  "refund.updated", "refund.failed"):
        assert event in guide, event


def test_guide_contains_no_real_secrets():
    guide = Path("docs/VM設置手順.md").read_text(encoding="utf-8")
    assert not re.search(r"sk_(test|live)_[A-Za-z0-9]{12,}|whsec_[A-Za-z0-9]{12,}", guide)


# ---- SQLite の設定 ----
def test_sqlite_uses_wal_for_file_databases(tmp_path, monkeypatch):
    from app.config import TestingConfig
    from app import create_app
    from app.extensions import db

    monkeypatch.setattr(TestingConfig, "SQLALCHEMY_DATABASE_URI", f"sqlite:///{tmp_path}/wal.db")
    app = create_app("testing")
    with app.app_context():
        mode = db.session.execute(db.text("PRAGMA journal_mode")).scalar()
        timeout = db.session.execute(db.text("PRAGMA busy_timeout")).scalar()
        db.session.remove()
    assert mode == "wal" and timeout == 15000


def test_in_memory_database_is_left_alone(app):
    from app.extensions import db

    if not str(db.engine.url).startswith("sqlite"):
        pytest.skip("PRAGMA は SQLite 専用")
    assert db.session.execute(db.text("PRAGMA journal_mode")).scalar() == "memory"
