import re

import pytest

from .conftest import login


# ---- スタイルシート ----
def test_stylesheet_is_served_without_login(client):
    res = client.get("/static/css/style.css")
    assert res.status_code == 200 and res.mimetype == "text/css"
    css = res.get_data(as_text=True)
    assert "--primary: #566900" in css  # ロゴの文字の、オリーブグリーン
    assert "--bg: #fdfbf4" in css  # ロゴの中の、クリーム色
    assert "prefers" not in css  # ダークモードなどは、まだ作っていない


def test_no_leftover_blue_palette():
    from pathlib import Path

    css = Path("app/static/css/style.css").read_text(encoding="utf-8").lower()
    for blue in ("#1f4e79", "#163a5a", "#eaf1f8", "#c6d6e6", "#7ba7d1"):
        assert blue not in css, blue


def test_text_and_buttons_have_enough_contrast():
    """文字と背景の、明るさの差（WCAG のコントラスト比）が、読める値であること。"""
    def luminance(hex_color):
        r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
        f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)

    def ratio(a, b):
        la, lb = sorted((luminance(a), luminance(b)), reverse=True)
        return (la + 0.05) / (lb + 0.05)

    assert ratio("#ffffff", "#566900") >= 4.5   # 緑のボタンの白い文字
    assert ratio("#566900", "#ffffff") >= 4.5   # 白地の、緑のリンク・見出し
    assert ratio("#566900", "#fdfbf4") >= 4.5   # クリーム地の、緑の文字
    assert ratio("#2f2a22", "#fdfbf4") >= 7     # 本文
    assert ratio("#6b6355", "#fdfbf4") >= 4.5   # 補助の文字
    assert ratio("#a4262c", "#fdecea") >= 4.5   # エラー
    assert ratio("#1f6a3a", "#e6f4ea") >= 4.5   # 成功


def test_stylesheet_braces_are_balanced():
    from pathlib import Path

    css = Path("app/static/css/style.css").read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    assert css.count("{") == css.count("}") and css.count("{") > 50


def test_every_color_used_comes_from_the_palette():
    """色は :root の1か所で決める。ほかの場所に、色の直書きが増えていないか（少数の例外のみ）。"""
    from pathlib import Path

    css = Path("app/static/css/style.css").read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    body = css.split("}", 1)[1]  # :root の外側
    direct = re.findall(r"#[0-9a-fA-F]{3,6}\b", body)
    assert len(direct) <= 12, direct


def test_stylesheet_has_styles_for_what_the_templates_use():
    from pathlib import Path

    css = Path("app/static/css/style.css").read_text(encoding="utf-8")
    for selector in (".site-header", ".brand", ".site-nav", ".nav-signup", ".flash", ".demo-banner", ".hero",
                     "button", "table", "textarea", "input[type=\"text\"]", "@media (max-width: 720px)", "@media print"):
        assert selector in css, selector


# ---- すべての画面が、スタイルシートを読み込む ----
def test_public_pages_link_the_stylesheet(client):
    for path in ("/", "/about", "/services", "/login", "/register", "/password/reset"):
        assert 'href="/static/css/style.css"' in client.get(path).get_data(as_text=True), path


def test_client_and_admin_pages_link_the_stylesheet(client, client_user, app, admin_user):
    login(client, "client@example.com")
    for path in ("/mypage/", "/releases/", "/guide", "/mypage/profile", "/mypage/orders", "/mypage/service"):
        assert 'href="/static/css/style.css"' in client.get(path).get_data(as_text=True), path
    admin = app.test_client()
    login(admin, "admin@example.com")
    for path in ("/admin/", "/admin/releases", "/admin/orders", "/admin/users", "/admin/services"):
        assert 'href="/static/css/style.css"' in admin.get(path).get_data(as_text=True), path


def test_error_pages_still_render(client):
    assert client.get("/no-such-page").status_code == 404


# ---- 画面の部品 ----
def test_header_structure(client):
    body = client.get("/").get_data(as_text=True)
    for expected in ('class="site-header"', 'class="site-header-inner"', 'class="brand"', 'class="site-nav"',
                     'class="nav-signup"'):
        assert expected in body, expected


def test_logout_button_is_inside_the_nav(client, client_user):
    login(client, "client@example.com")
    body = client.get("/mypage/").get_data(as_text=True)
    nav = body[body.index('class="site-nav"'):body.index("</nav>")]
    assert "ログアウト" in nav and "nav-signup" not in nav


def test_top_page_has_hero(client):
    assert 'class="hero"' in client.get("/").get_data(as_text=True)


def test_flash_messages_use_the_flash_class(client, client_user):
    res = client.post("/login", data={"email": "client@example.com", "password": "wrong-password"})
    body = res.get_data(as_text=True)
    assert 'class="flash"' in body and 'class="error"' in body


def test_demo_banner_uses_a_class_not_inline_style(client, app):
    app.config["DEMO_MODE"] = True
    body = client.get("/").get_data(as_text=True)
    assert 'class="demo-banner"' in body
    assert 'style="background:#fff3cd' not in body


# ---- ロゴ ----
def test_yukiwari_logo_is_shown_in_the_header_by_default(client):
    body = client.get("/").get_data(as_text=True)
    header = body[body.index('class="brand"'):body.index("</a>", body.index('class="brand"'))]
    assert '<img src="/static/img/logo.webp" alt="ゆきわり。">' in header and "プレスリリースサポート" in header


def test_logo_file_exists_and_is_served(client):
    res = client.get("/static/img/logo.webp")
    assert res.status_code == 200 and res.mimetype == "image/webp" and len(res.data) > 10_000


def test_logo_is_a_reasonable_size_for_the_web():
    from pathlib import Path

    from PIL import Image

    path = Path("app/static/img/logo.webp")
    assert path.stat().st_size < 300 * 1024  # 重すぎない
    with Image.open(path) as im:
        assert im.width >= 800 and 2.0 < im.width / im.height < 3.5  # 横長のバナー（元のロゴの比率）


def test_original_logo_is_kept_outside_the_static_folder():
    from pathlib import Path

    assert Path("design/yukiwari-logo-original.jpg").is_file()
    assert not list(Path("app/static").rglob("yukiwari-logo-original*"))


def test_no_logo_when_the_file_is_missing(app, client, tmp_path):
    app.static_folder = str(tmp_path / "no-static")
    body = client.get("/").get_data(as_text=True)
    header = body[body.index('class="brand"'):body.index("</a>", body.index('class="brand"'))]
    assert "<img" not in header and "プレスリリースサポート" in header


@pytest.mark.parametrize("ext", ["png", "svg", "webp", "jpg"])
def test_logo_appears_when_a_file_is_placed(app, client, tmp_path, ext):
    static = tmp_path / "static"
    (static / "img").mkdir(parents=True)
    (static / "img" / f"logo.{ext}").write_bytes(b"x")
    app.static_folder = str(static)
    body = client.get("/").get_data(as_text=True)
    assert f'<img src="/static/img/logo.{ext}"' in body


def test_logo_does_not_break_pages_when_missing(app, client, tmp_path):
    app.static_folder = str(tmp_path / "does-not-exist")
    assert client.get("/").status_code == 200


def test_big_logo_is_on_the_top_page_only(client):
    assert 'class="hero-logo"' in client.get("/").get_data(as_text=True)
    assert 'class="hero-logo"' not in client.get("/about").get_data(as_text=True)


# ---- ブラウザに保存させる ----
def test_static_files_are_cacheable_but_pages_are_not_for_logged_in_users(client, client_user):
    login(client, "client@example.com")
    assert "no-store" not in client.get("/static/css/style.css").headers.get("Cache-Control", "")
    assert "no-store" not in client.get("/static/img/logo.webp").headers.get("Cache-Control", "")
    assert "no-store" in client.get("/mypage/").headers["Cache-Control"]
