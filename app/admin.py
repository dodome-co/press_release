"""管理者の画面：書類の一覧・確認・編集・承認・差し戻し。"""
import io

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user

from .auth.decorators import admin_required
from .extensions import db
from .models import (
    MAX_PHOTOS,
    MAX_SCHEDULE_ITEMS,
    RELEASE_APPROVED,
    RELEASE_PENDING,
    RELEASE_STATUSES,
    Release,
    ReleasePhoto,
)
from .releases import notifications, service
from .releases.forms import ReleaseForm
from .uploads import UploadError, resolve_path
from wtforms import TextAreaField
from wtforms.validators import Length

bp = Blueprint("admin", __name__, url_prefix="/admin")


class AdminReleaseForm(ReleaseForm):
    summary = TextAreaField("要旨（前文）", validators=[Length(max=2000)])


def _release_or_404(release_id: int) -> Release:
    release = db.session.get(Release, release_id)
    if release is None or release.status == "draft":  # 下書きは、提出されるまで管理者にも見せない
        abort(404)
    return release


@bp.get("/releases")
@admin_required
def releases():
    status = request.args.get("status", "")
    query = Release.query.filter(Release.status != "draft")
    if status in RELEASE_STATUSES:
        query = query.filter_by(status=status)
    if status == RELEASE_PENDING or not status:
        # 承認待ちを、古いものから順に（待たせている順）
        query = query.order_by(
            (Release.status != RELEASE_PENDING), Release.submitted_at.asc(), Release.id.asc()
        )
    else:
        query = query.order_by(Release.updated_at.desc())
    return render_template("admin/releases.html", releases=query.all(), status=status)


@bp.route("/releases/<int:release_id>", methods=["GET", "POST"])
@admin_required
def release_detail(release_id):
    release = _release_or_404(release_id)
    editable = release.status == RELEASE_PENDING
    form = AdminReleaseForm(obj=release if request.method == "GET" else None)
    schedule = [(i.time_text, i.content) for i in release.schedule_items]
    if request.method == "POST":
        if not editable:
            flash("承認待ちの書類だけ、編集できます。", "error")
            return redirect(url_for("admin.release_detail", release_id=release.id))
        if form.validate_on_submit():
            values = {name: getattr(form, name).data for name in service.ADMIN_FIELDS}
            try:
                changed = service.admin_update(
                    release, current_user, values, form.schedule_rows(), form.summary.data
                )
            except service.ReleaseError as e:
                flash(str(e), "error")
                return redirect(url_for("admin.release_detail", release_id=release.id))
            flash("保存しました。" if changed else "変更はありませんでした。", "success")
            return redirect(url_for("admin.release_detail", release_id=release.id))
        schedule = form.schedule_rows()
    schedule = (list(schedule) + [("", "")] * MAX_SCHEDULE_ITEMS)[:MAX_SCHEDULE_ITEMS]
    return render_template(
        "admin/release_detail.html",
        release=release,
        form=form,
        schedule=schedule,
        editable=editable,
        max_photos=MAX_PHOTOS,
    )


def _guard(action):
    """サービスの規則違反（承認待ちでない、など）を、画面のメッセージにする。"""
    try:
        action()
        return True
    except (service.ReleaseError, UploadError) as e:
        flash(str(e), "error")
        return False


@bp.post("/releases/<int:release_id>/photos")
@admin_required
def add_photo(release_id):
    release = _release_or_404(release_id)
    file = request.files.get("photo")
    if file is None or not file.filename:
        flash("写真を選択してください。", "error")
    else:
        if _guard(lambda: service.admin_add_photo(release, current_user, file)):
            flash("写真を追加しました。", "success")
    return redirect(url_for("admin.release_detail", release_id=release.id))


@bp.post("/releases/<int:release_id>/photos/<int:photo_id>/delete")
@admin_required
def delete_photo(release_id, photo_id):
    release = _release_or_404(release_id)
    photo = db.session.get(ReleasePhoto, photo_id)
    if photo is None or photo.release_id != release.id:
        abort(404)
    if _guard(lambda: service.admin_delete_photo(release, current_user, photo)):
        flash("写真を削除しました。", "success")
    return redirect(url_for("admin.release_detail", release_id=release.id))


@bp.post("/releases/<int:release_id>/approve")
@admin_required
def approve(release_id):
    release = _release_or_404(release_id)
    if _guard(lambda: service.approve(release, current_user)):
        notifications.notify_approved(release)
        flash("承認しました。Word を出力しました。", "success")
    return redirect(url_for("admin.release_detail", release_id=release.id))


@bp.post("/releases/<int:release_id>/reject")
@admin_required
def reject(release_id):
    release = _release_or_404(release_id)
    if _guard(lambda: service.reject(release, current_user, request.form.get("reason", ""))):
        notifications.notify_rejected(release)
        flash("差し戻しました。", "success")
    return redirect(url_for("admin.release_detail", release_id=release.id))


@bp.get("/releases/<int:release_id>/preview.docx")
@admin_required
def preview_docx(release_id):
    """承認前に Word の仕上がりを見るための仮出力。保存せず、承認もしない。"""
    from . import docgen

    release = _release_or_404(release_id)
    if release.status != RELEASE_PENDING:
        abort(404)
    try:
        data = docgen.render_release(release)
    except Exception:
        current_app.logger.exception("仮出力に失敗しました（書類ID=%s）", release.id)
        flash("Word を作れませんでした。", "error")
        return redirect(url_for("admin.release_detail", release_id=release.id))
    return send_file(
        io.BytesIO(data),
        as_attachment=True,
        download_name=f"press_release_{release.id}_preview.docx",
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


@bp.get("/releases/<int:release_id>/download")
@admin_required
def download(release_id):
    release = _release_or_404(release_id)
    return send_approved_docx(release)


def send_approved_docx(release: Release):
    """承認済みの Word を返す。承認前や、ファイルがないときは 404。"""
    if release.status != RELEASE_APPROVED or not release.file_path:
        abort(404)
    try:
        path = resolve_path(release.file_path)
    except UploadError:
        abort(404)
    if not path.is_file():
        abort(404)
    response = send_file(
        path,
        as_attachment=True,
        download_name=f"press_release_{release.id}.docx",
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response
