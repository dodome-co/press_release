from flask import abort, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user

from .. import ai, ratelimit
from ..auth.decorators import client_required
from ..extensions import db
from ..models import MAX_PHOTOS, MAX_SCHEDULE_ITEMS, Release, ReleasePhoto
from ..uploads import UploadError
from . import bp, chat, notifications, service
from .forms import ReleaseForm


def _owned_release_or_404(release_id: int) -> Release:
    """自分の書類だけを返す。他人の書類は、存在の有無が分からないよう 404 にする。"""
    release = db.session.get(Release, release_id)
    if release is None or release.user_id != current_user.id:
        abort(404)
    return release


def _form_values(form: ReleaseForm) -> dict:
    return {name: getattr(form, name).data for name in service.CLIENT_FIELDS}


@bp.get("/")
@client_required
def index():
    releases = (
        Release.query.filter_by(user_id=current_user.id).order_by(Release.created_at.desc()).all()
    )
    return render_template(
        "releases/index.html",
        releases=releases,
        can_create=service.can_create(current_user),
        free_limit=service.free_limit(),
    )


@bp.post("/new")
@client_required
def create():
    try:
        release = service.create_draft(current_user)
    except service.ReleaseError as e:
        flash(str(e), "error")
        return redirect(url_for("releases.index"))
    return redirect(url_for("releases.edit", release_id=release.id))


@bp.route("/<int:release_id>/edit", methods=["GET", "POST"])
@client_required
def edit(release_id):
    release = _owned_release_or_404(release_id)
    if not service.client_can_edit(release):
        flash("この書類は、いまは編集できません。", "error")
        return redirect(url_for("releases.view", release_id=release.id))
    form = ReleaseForm(obj=release if request.method == "GET" else None)
    schedule = [(i.time_text, i.content) for i in release.schedule_items]
    if request.method == "POST":
        if form.validate_on_submit():
            try:
                service.update_fields(release, _form_values(form))
                service.replace_schedule(release, form.schedule_rows())
                db.session.commit()
            except service.ReleaseError as e:  # 保存の直前に、管理者の操作などで状態が変わったとき
                flash(str(e), "error")
                return redirect(url_for("releases.view", release_id=release.id))
            flash("保存しました。", "success")
            return redirect(url_for("releases.edit", release_id=release.id))
        schedule = form.schedule_rows()
    schedule = (list(schedule) + [("", "")] * MAX_SCHEDULE_ITEMS)[:MAX_SCHEDULE_ITEMS]
    return render_template(
        "releases/edit.html",
        form=form,
        release=release,
        schedule=schedule,
        max_photos=MAX_PHOTOS,
        missing=service.missing_required(release),
        ai_ready=ai.is_configured(),
        first_question=ai.FIRST_QUESTION,
        chat_remaining=chat.remaining_calls(release),
        latest_start_label=chat.latest_start_label(),
    )


@bp.post("/<int:release_id>/chat")
@client_required
def chat_send(release_id):
    """AI とのチャット（画面の JavaScript から呼ぶ）。答えは JSON で返す。"""
    release = _owned_release_or_404(release_id)
    if not ratelimit.check_and_record(ratelimit.ai_chat_per_user(), f"user:{current_user.id}"):
        return jsonify(error="送る回数が多すぎます。しばらくたってから、もう一度お試しください。"), 429
    data = request.get_json(silent=True) or {}
    try:
        result = chat.send(release, data.get("message", ""), data.get("input_method", "text"))
    except chat.ChatError as e:
        return jsonify(error=str(e)), e.status
    return jsonify(result)


@bp.post("/<int:release_id>/photos")
@client_required
def upload_photo(release_id):
    release = _owned_release_or_404(release_id)
    file = request.files.get("photo")
    if file is None or not file.filename:
        flash("写真を選択してください。", "error")
    else:
        try:
            service.add_photo(release, file)
            flash("写真を追加しました。", "success")
        except (service.ReleaseError, UploadError) as e:
            flash(str(e), "error")
    return redirect(url_for("releases.edit", release_id=release.id))


@bp.post("/<int:release_id>/photos/<int:photo_id>/delete")
@client_required
def delete_photo(release_id, photo_id):
    release = _owned_release_or_404(release_id)
    photo = db.session.get(ReleasePhoto, photo_id)
    if photo is None or photo.release_id != release.id:
        abort(404)
    try:
        service.delete_photo(release, photo)
        flash("写真を削除しました。", "success")
    except service.ReleaseError as e:
        flash(str(e), "error")
    return redirect(url_for("releases.edit", release_id=release.id))


@bp.get("/<int:release_id>")
@client_required
def view(release_id):
    """プレビュー。入力内容を HTML で確認する（Word は承認時に出力される）。"""
    release = _owned_release_or_404(release_id)
    return render_template(
        "releases/view.html",
        release=release,
        can_edit=service.client_can_edit(release),
        can_delete=service.client_can_delete(release),
        missing=service.missing_required(release),
        target_days=current_app.config["APPROVAL_TARGET_BUSINESS_DAYS"],
        admin_changes=[e for e in release.edits if e.field_name != "summary"],
        field_labels=service.FIELD_LABELS,
    )


@bp.post("/<int:release_id>/submit")
@client_required
def submit(release_id):
    release = _owned_release_or_404(release_id)
    try:
        service.submit(release)
        notifications.notify_submitted(release)
        flash("提出しました。管理者が確認します。", "success")
    except service.ReleaseError as e:
        flash(str(e), "error")
    return redirect(url_for("releases.view", release_id=release.id))


@bp.post("/<int:release_id>/delete")
@client_required
def delete(release_id):
    release = _owned_release_or_404(release_id)
    try:
        service.delete_draft(release)
        flash("下書きを削除しました。", "success")
        return redirect(url_for("releases.index"))
    except service.ReleaseError as e:
        flash(str(e), "error")
        return redirect(url_for("releases.view", release_id=release.id))


@bp.get("/<int:release_id>/download")
@client_required
def download(release_id):
    """承認された書類の Word をダウンロードする（承認前は 404）。"""
    from ..admin import send_approved_docx

    release = _owned_release_or_404(release_id)
    return send_approved_docx(release)
