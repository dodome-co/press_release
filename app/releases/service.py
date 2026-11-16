"""書類（プレスリリース）の規則。状態の変更や無料枠の判定は、必ずここを通す。

画面（routes）から直接 status を書き換えない。規則を1か所にまとめることで、
抜け道ができるのを防ぐ。
"""
from flask import current_app

from .. import uploads
from ..extensions import db
from ..models import (
    MAX_PHOTOS,
    RELEASE_DRAFT,
    RELEASE_PENDING,
    RELEASE_REJECTED,
    Release,
    ReleasePhoto,
    ReleaseScheduleItem,
    utcnow,
)


class ReleaseError(Exception):
    """利用者に見せてよいメッセージを持つエラー。"""


# 提出時に必ず埋まっている必要がある項目（項目名 → 表示名）
REQUIRED_FOR_SUBMIT = {
    "title": "タイトル",
    "event_at_text": "日時",
    "location": "場所",
    "service_description": "サービスや商品の概要",
    "feature_1": "特徴1",
    "application_method": "取材の申込方法",
    "application_deadline": "申込の締切",
    "source_what": "取材してほしい内容",
    "source_why": "なぜ今これを行うのか",
    "source_message": "伝えたいこと",
}

# クライアントが編集できる項目
CLIENT_FIELDS = (
    "title", "event_at_text", "location", "service_description",
    "feature_1", "feature_2", "feature_3",
    "application_method", "application_deadline",
    "source_what", "source_why", "source_message",
)


# ---- 無料枠 ----
def count_first_submitted(user_id: int) -> int:
    """初めて提出した書類の数（無料枠の数え方）。差し戻し後の再提出は数えない。"""
    return Release.query.filter(
        Release.user_id == user_id, Release.first_submitted_at.isnot(None)
    ).count()


def free_limit() -> int:
    return current_app.config["FREE_RELEASE_LIMIT"]


def can_create(user) -> bool:
    return count_first_submitted(user.id) < free_limit()


# ---- 編集できるか ----
def client_can_edit(release: Release) -> bool:
    """クライアントが編集できるのは、下書きと差し戻しのときだけ。"""
    return release.status in (RELEASE_DRAFT, RELEASE_REJECTED)


def client_can_delete(release: Release) -> bool:
    """削除できるのは下書きだけ。提出済みの書類は削除できない。"""
    return release.status == RELEASE_DRAFT


def _ensure_editable(release: Release) -> None:
    if not client_can_edit(release):
        raise ReleaseError("この書類は、いまは編集できません。")


# ---- 作成・更新・削除 ----
def create_draft(user) -> Release:
    if not can_create(user):
        raise ReleaseError(
            f"無料で作成できる書類は{free_limit()}件までです。有料サービスをご利用ください。"
        )
    release = Release(user_id=user.id)
    db.session.add(release)
    db.session.commit()
    return release


def update_fields(release: Release, values: dict) -> None:
    """入力項目を更新する（クライアント用）。値は前後の空白を取る。"""
    _ensure_editable(release)
    for name in CLIENT_FIELDS:
        if name in values:
            setattr(release, name, (values[name] or "").strip().replace("\r\n", "\n"))


def replace_schedule(release: Release, rows: list[tuple[str, str]]) -> None:
    """当日のスケジュールを入れ替える。空の行は除き、最大5行。"""
    _ensure_editable(release)
    cleaned = [(t.strip(), c.strip()) for t, c in rows if t.strip() or c.strip()]
    release.schedule_items.clear()
    db.session.flush()
    for position, (time_text, content) in enumerate(cleaned, start=1):
        release.schedule_items.append(
            ReleaseScheduleItem(position=position, time_text=time_text, content=content)
        )


def add_photo(release: Release, file_storage) -> ReleasePhoto:
    _ensure_editable(release)
    used = {p.position for p in release.photos}
    free = [pos for pos in range(1, MAX_PHOTOS + 1) if pos not in used]
    if not free:
        raise ReleaseError(f"写真は{MAX_PHOTOS}枚までです。")
    saved = uploads.save_image(file_storage, f"releases/{release.id}/photos")
    photo = ReleasePhoto(
        release_id=release.id,
        position=free[0],
        file_path=saved.relative_path,
        original_filename=saved.original_filename,
        content_type=saved.content_type,
        size_bytes=saved.size_bytes,
    )
    db.session.add(photo)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        uploads.delete_file(saved.relative_path)  # DB に残らないファイルは消す
        raise
    return photo


def delete_photo(release: Release, photo: ReleasePhoto) -> None:
    _ensure_editable(release)
    path = photo.file_path
    db.session.delete(photo)
    db.session.commit()
    uploads.delete_file(path)


def delete_draft(release: Release) -> None:
    if not client_can_delete(release):
        raise ReleaseError("提出済みの書類は削除できません。")
    paths = [p.file_path for p in release.photos]
    db.session.delete(release)
    db.session.commit()
    for path in paths:
        uploads.delete_file(path)


# ---- 提出 ----
def missing_required(release: Release) -> list[str]:
    return [label for name, label in REQUIRED_FOR_SUBMIT.items() if not (getattr(release, name) or "").strip()]


def submit(release: Release) -> None:
    """承認待ちにする。必須項目と無料枠を確認する。"""
    _ensure_editable(release)
    missing = missing_required(release)
    if missing:
        raise ReleaseError("次の項目を入力してください：" + "、".join(missing))
    first_time = release.first_submitted_at is None
    if first_time and not can_create(release.user):
        raise ReleaseError(
            f"無料で提出できる書類は{free_limit()}件までです。有料サービスをご利用ください。"
        )
    now = utcnow()
    if first_time:
        release.first_submitted_at = now  # 一度入れたら変えない
    release.submitted_at = now
    release.status = RELEASE_PENDING
    db.session.commit()
