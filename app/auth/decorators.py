from functools import wraps

from flask import abort
from flask_login import current_user, login_required


def admin_required(view):
    """管理者だけが入れる画面。未ログインはログイン画面へ、権限がなければ 403。"""

    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def client_required(view):
    """クライアント専用の画面。管理者は入れない（403）。"""

    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapped
