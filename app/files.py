"""アップロードされたファイルの配信。

URL を直接開けるようにはせず、必ず「本人か管理者か」を確認してから返す。
"""
from flask import Blueprint, abort, send_file
from flask_login import current_user, login_required

from .extensions import db
from .models import ReleasePhoto, User
from .uploads import UploadError, resolve_path

bp = Blueprint("files", __name__, url_prefix="/files")


def _send(relative_path: str, content_type: str):
    try:
        path = resolve_path(relative_path)
    except UploadError:
        abort(404)
    if not path.is_file():
        abort(404)
    response = send_file(path, mimetype=content_type, conditional=True)
    response.headers["Content-Disposition"] = "inline"
    response.headers["Cache-Control"] = "private, no-store"
    return response


@bp.get("/photos/<int:photo_id>")
@login_required
def photo(photo_id):
    photo = db.session.get(ReleasePhoto, photo_id)
    if photo is None:
        abort(404)
    if not current_user.is_admin and photo.release.user_id != current_user.id:
        abort(404)  # 他人のファイルは、存在の有無も分からないようにする
    return _send(photo.file_path, photo.content_type)


@bp.get("/logos/<int:user_id>")
@login_required
def logo(user_id):
    user = db.session.get(User, user_id)
    if user is None or not user.logo_path:
        abort(404)
    if not current_user.is_admin and user.id != current_user.id:
        abort(404)
    suffix = user.logo_path.rsplit(".", 1)[-1].lower()
    content_type = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(suffix)
    if content_type is None:
        abort(404)
    return _send(user.logo_path, content_type)
