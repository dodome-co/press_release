"""書類（プレスリリース）の規則。状態の変更や無料枠の判定は、必ずここを通す。

画面（routes）から直接 status を書き換えない。規則を1か所にまとめることで、
抜け道ができるのを防ぐ。
"""
from flask import current_app
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from .. import uploads
from ..extensions import db
from ..services_catalog import RELEASE_CREATION
from ..textutil import clean_text
from ..models import (
    MAX_PHOTOS,
    ORDER_PAID,
    Order,
    RELEASE_APPROVED,
    RELEASE_DRAFT,
    RELEASE_PENDING,
    RELEASE_REJECTED,
    Release,
    ReleaseEdit,
    ReleasePhoto,
    User,
    utcnow,
)


class ReleaseError(Exception):
    """利用者に見せてよいメッセージを持つエラー。"""


# 提出時に必ず埋まっている必要がある項目（項目名 → 表示名）
# 日付・時刻・場所は、フォームの入力。特徴と要旨の元になる情報は、AI とのチャットで集まる（チャットが終わると入る）
REQUIRED_FOR_SUBMIT = {
    "event_date": "取材会の日付",
    "event_start_time": "開始時刻",
    "location": "場所",
    "feature_1": "特徴（AI とのチャットで入ります）",
    "source_what": "取材してほしい内容（AI とのチャットで入ります）",
    "source_why": "なぜ今これを行うのか（AI とのチャットで入ります）",
    "source_message": "伝えたいこと（AI とのチャットで入ります）",
}
# 承認のときに、さらに必要な項目（管理者が決める・仕上げる）
REQUIRED_FOR_APPROVE = {"title": "タイトル", "summary": "要旨（前文）"}

# 日付・時刻の項目（文字列ではなく、日付型・時刻型で持つ）
DATE_FIELDS = ("event_date", "event_start_time", "event_end_time", "application_deadline_at")


def _display(release: Release, name: str) -> str:
    """編集履歴に残す、項目の値の文字列。"""
    if name == "event_date":
        return release.event_at_display
    if name == "application_deadline_at":
        return release.application_deadline_display
    value = getattr(release, name)
    if name in ("event_start_time", "event_end_time"):
        return "" if value is None else value.strftime("%H:%M")
    return value or ""


# クライアントがフォームで編集できる項目
CLIENT_FIELDS = ("event_date", "event_start_time", "event_end_time", "location")


# ---- 排他（同時の操作で状態が食い違わないようにする） ----
EDITABLE = (RELEASE_DRAFT, RELEASE_REJECTED)  # クライアントが編集できる状態
PENDING = (RELEASE_PENDING,)  # 管理者が操作できる状態


def _fail(message: str):
    """途中の変更とロックを捨てて、利用者向けのエラーにする。"""
    db.session.rollback()
    raise ReleaseError(message)


def _lock(release: Release, allowed_statuses: tuple, message: str) -> None:
    """書類の行をロックし、状態が期待どおりかを確認して、最新の内容を読み直す。

    「状態が allowed のときだけ更新する」という1回の UPDATE で行うので、同時に来た
    別の操作（提出と編集、承認と承認など）は、先の操作が終わるまで待たされる。
    """
    rows = db.session.execute(
        update(Release)
        .where(Release.id == release.id, Release.status.in_(allowed_statuses))
        .values(updated_at=utcnow())
    ).rowcount
    if rows != 1:
        _fail(message)
    db.session.refresh(release)


def _lock_user(user_id: int) -> None:
    """ユーザーの行をロックする。同じ人の無料枠の確認が、同時に走らないようにする。"""
    db.session.execute(update(User).where(User.id == user_id).values(updated_at=utcnow()))


def _lock_editable(release: Release) -> None:
    _lock(release, EDITABLE, "この書類は、いまは編集できません。")


def _lock_pending(release: Release) -> None:
    _lock(release, PENDING, "承認待ちの書類だけ、この操作ができます。")


# ---- 無料枠 ----
def count_first_submitted(user_id: int) -> int:
    """初めて提出した書類の数（無料枠の数え方）。差し戻し後の再提出は数えない。"""
    return Release.query.filter(
        Release.user_id == user_id, Release.first_submitted_at.isnot(None)
    ).count()


def count_drafts(user_id: int) -> int:
    return Release.query.filter_by(user_id=user_id, status=RELEASE_DRAFT).count()


def free_limit() -> int:
    return current_app.config["FREE_RELEASE_LIMIT"]


def count_purchased_slots(user_id: int) -> int:
    """購入した「リリース作成の追加」の枠の数（支払済みの注文の数）。返金済みは数えない。"""
    # 返金の手続きに入った（返金額を確保した）ものは、数えない。返金が失敗して戻れば、また数える
    return Order.query.filter_by(
        user_id=user_id, service_type=RELEASE_CREATION, status=ORDER_PAID, refunded_amount=0
    ).count()


def total_slots(user_id: int) -> int:
    """初めて提出できる書類の数 ＝ 無料の枠 ＋ 購入した枠。"""
    return free_limit() + count_purchased_slots(user_id)


def can_create(user) -> bool:
    return count_first_submitted(user.id) < total_slots(user.id)


# ---- 編集できるか（画面の表示用。実際の更新時は、上の _lock で改めて確認する） ----
def client_can_edit(release: Release) -> bool:
    """クライアントが編集できるのは、下書きと差し戻しのときだけ。"""
    return release.status in EDITABLE


def client_can_delete(release: Release) -> bool:
    """削除できるのは下書きだけ。提出済みの書類は削除できない。"""
    return release.status == RELEASE_DRAFT


# ---- 作成・更新・削除 ----
def create_draft(user) -> Release:
    _lock_user(user.id)
    if not can_create(user):
        _fail("無料で作成できる書類の枠を使い切りました。「2件目以降のリリース作成」をご購入ください。")
    limit = current_app.config["DRAFT_LIMIT"]
    if count_drafts(user.id) >= limit:
        _fail(f"下書きは{limit}件までです。不要な下書きを削除するか、提出してください。")
    release = Release(user_id=user.id)
    db.session.add(release)
    db.session.commit()
    return release


def _check_title(title: str | None) -> None:
    from .forms import title_length_error

    message = title_length_error(title)
    if message:
        _fail(message)


def update_fields(release: Release, values: dict) -> None:
    """入力項目を更新する（クライアント用）。画面の保存処理が commit する。"""
    _lock_editable(release)
    for name in CLIENT_FIELDS:
        if name in values:
            setattr(release, name, values[name] if name in DATE_FIELDS else clean_text(values[name]))


def _free_position(release: Release) -> int:
    used = {p.position for p in release.photos}
    free = [pos for pos in range(1, MAX_PHOTOS + 1) if pos not in used]
    if not free:
        _fail(f"写真は{MAX_PHOTOS}枚までです。")
    return free[0]


def _store_photo(release: Release, file_storage) -> ReleasePhoto:
    """写真を保存して、DB に足す（commit は呼び出し側）。DB に残らないファイルは消す。"""
    position = _free_position(release)
    saved = uploads.save_image(file_storage, f"releases/{release.id}/photos", require_landscape=True)
    photo = ReleasePhoto(
        release_id=release.id,
        position=position,
        file_path=saved.relative_path,
        original_filename=saved.original_filename,
        content_type=saved.content_type,
        size_bytes=saved.size_bytes,
    )
    db.session.add(photo)
    try:
        db.session.flush()
    except IntegrityError:
        uploads.delete_file(saved.relative_path)
        _fail("写真を追加できませんでした。もう一度お試しください。")
    return photo


def add_photo(release: Release, file_storage) -> ReleasePhoto:
    _lock_editable(release)
    photo = _store_photo(release, file_storage)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        uploads.delete_file(photo.file_path)
        raise
    return photo


def delete_photo(release: Release, photo: ReleasePhoto) -> None:
    _lock_editable(release)
    path = photo.file_path
    db.session.delete(photo)
    db.session.commit()
    uploads.delete_file(path)


def delete_draft(release: Release) -> None:
    _lock(release, (RELEASE_DRAFT,), "提出済みの書類は削除できません。")
    paths = [p.file_path for p in release.photos]
    db.session.delete(release)
    db.session.commit()
    for path in paths:
        uploads.delete_file(path)


# ---- 提出 ----
def _empty(release: Release, name: str) -> bool:
    value = getattr(release, name)
    return value is None if name in DATE_FIELDS else not (value or "").strip()


def missing_required(release: Release) -> list[str]:
    """提出に足りない項目。"""
    return [label for name, label in REQUIRED_FOR_SUBMIT.items() if _empty(release, name)]


def missing_for_approval(release: Release) -> list[str]:
    """承認に足りない項目（提出に必要な項目と、タイトル・要旨）。"""
    names = {**REQUIRED_FOR_SUBMIT, **REQUIRED_FOR_APPROVE}
    return [label for name, label in names.items() if _empty(release, name)]


def submit(release: Release) -> None:
    """承認待ちにする。必須項目と無料枠を確認する。"""
    _lock_editable(release)
    missing = missing_required(release)
    if missing:
        _fail("次の項目を入力してください：" + "、".join(missing))
    first_time = release.first_submitted_at is None
    if first_time:
        _lock_user(release.user_id)  # 同じ人の2件が同時に提出されても、1件ずつ確認する
        if not can_create(release.user):
            _fail("無料で提出できる書類の枠を使い切りました。「2件目以降のリリース作成」をご購入ください。")
    now = utcnow()
    if first_time:
        release.first_submitted_at = now  # 一度入れたら変えない
    release.submitted_at = now
    release.status = RELEASE_PENDING
    db.session.commit()


# ---- 管理者の操作（承認待ちの書類だけ） ----
ADMIN_FIELDS = (
    "title", "subtitle", "event_date", "event_start_time", "event_end_time", "location",
    "feature_1", "feature_2", "feature_3", "speakers",
)
FIELD_LABELS = {
    **REQUIRED_FOR_SUBMIT,
    **REQUIRED_FOR_APPROVE,
    "event_end_time": "終了時刻",
    "feature_2": "特徴2",
    "feature_3": "特徴3",
    "subtitle": "サブタイトル",
    "speakers": "当日の対応者",
    "summary": "要旨（前文）",
    "photos": "写真",
}


def _record_edit(release: Release, admin, field: str, old: str | None, new: str | None) -> None:
    if (old or "") == (new or ""):
        return
    release.edits.append(
        ReleaseEdit(edited_by=admin.id, field_name=field, old_value=old or "", new_value=new or "")
    )


def _apply_edits(release: Release, editor, values: dict, summary: str) -> list[str]:
    """項目を直す。変わった項目だけを、編集履歴に残し、変えた項目名の一覧を返す（commit は呼び出し側）。"""
    if "title" in values:
        _check_title(values["title"])
    changed: list[str] = []

    def apply(field: str, new_value) -> None:
        old = getattr(release, field)
        if field not in DATE_FIELDS:
            old = old or ""
        if old != new_value:
            old_text = _display(release, field)
            setattr(release, field, new_value)
            _record_edit(release, editor, field, old_text, _display(release, field))
            changed.append(field)

    for name in ADMIN_FIELDS:
        if name in values:
            apply(name, values[name] if name in DATE_FIELDS else clean_text(values[name]))
    apply("summary", clean_text(summary))
    if changed:
        release.last_edited_by = editor.id
        release.last_edited_at = utcnow()
    return changed


def admin_update(release: Release, admin, values: dict, summary: str) -> list[str]:
    """管理者が書類を編集する（承認待ちの書類だけ）。変えた項目名の一覧を返す。"""
    _lock_pending(release)
    changed = _apply_edits(release, admin, values, summary)
    db.session.commit()
    return changed


def client_revise(release: Release, user, values: dict, summary: str) -> list[str]:
    """承認されたあと、ダウンロードする前に、クライアントが内容を直す。直したら、PDF を作り直す。

    承認の日時などは変えない。誰が・いつ・何を直したかは、編集履歴に残る（管理者の画面で見られる）。
    """
    from .. import pdfgen

    _lock(release, (RELEASE_APPROVED,), "承認された書類だけ、この画面で直せます。")
    changed = _apply_edits(release, user, values, summary)
    if not changed:
        db.session.rollback()
        return []
    new_path = None
    try:
        new_path = pdfgen.write_release(release)
        release.file_path = new_path
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("PDF の作り直しに失敗したため、修正を取り消しました（書類ID=%s）", release.id)
        raise ReleaseError("PDF を作り直せなかったため、修正できませんでした。もう一度お試しください。") from e
    return changed


def admin_add_photo(release: Release, admin, file_storage) -> ReleasePhoto:
    _lock_pending(release)
    photo = _store_photo(release, file_storage)
    _record_edit(release, admin, "photos", "", f"追加：{photo.original_filename}")
    release.last_edited_by, release.last_edited_at = admin.id, utcnow()
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        uploads.delete_file(photo.file_path)
        raise
    return photo


def admin_delete_photo(release: Release, admin, photo: ReleasePhoto) -> None:
    _lock_pending(release)
    path, name = photo.file_path, photo.original_filename
    db.session.delete(photo)
    _record_edit(release, admin, "photos", f"削除：{name}", "")
    release.last_edited_by, release.last_edited_at = admin.id, utcnow()
    db.session.commit()
    uploads.delete_file(path)


def approve(release: Release, admin) -> None:
    """承認する。要旨が空だと承認できない。承認と PDF の出力は、まとめて成功か失敗かにする。"""
    from .. import pdfgen

    _lock_pending(release)  # 同時に承認・差し戻しが来ても、1つだけが通る
    if not (release.summary or "").strip():
        _fail("要旨（前文）を入力してから承認してください。")
    missing = missing_for_approval(release)
    if missing:
        _fail("次の項目が空です：" + "、".join(missing))

    relative_path = None
    try:
        release.approved_by = admin.id
        release.approved_at = utcnow()
        release.status = RELEASE_APPROVED
        relative_path = pdfgen.write_release(release)  # 失敗したら、下で全部元に戻す
        release.file_path = relative_path
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        uploads.delete_file(relative_path)
        current_app.logger.exception("PDF の出力に失敗したため、承認を取り消しました（書類ID=%s）", release.id)
        raise ReleaseError("PDF の出力に失敗したため、承認できませんでした。もう一度お試しください。") from e


def reject(release: Release, admin, reason: str) -> None:
    reason = clean_text(reason)
    if not reason:
        raise ReleaseError("差し戻しの理由を入力してください。")
    limit = current_app.config["REJECT_REASON_MAX_LENGTH"]
    if len(reason) > limit:
        raise ReleaseError(f"差し戻しの理由は{limit}文字以内にしてください。")
    _lock_pending(release)
    release.status = RELEASE_REJECTED
    release.reject_reason = reason
    db.session.commit()
