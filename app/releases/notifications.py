"""書類に関するメール通知（開発ではログに出る。本番の送信方法は未決）。"""
from flask import current_app

from ..mail import send_mail
from ..models import ROLE_ADMIN, Release, User
from ..security import external_url
from .service import FIELD_LABELS


def _send(to: str, subject: str, body: str) -> None:
    """メールの送信に失敗しても、書類の状態の変更（すでに完了している）を巻き戻さない。"""
    try:
        send_mail(to, subject, body)
    except Exception:
        current_app.logger.exception("メールの送信に失敗しました（件名=%s）", subject)


def notify_submitted(release: Release) -> None:
    """承認待ちが出たことを、管理者全員に知らせる（F25）。"""
    link = external_url("admin.release_detail", release_id=release.id)
    for admin in User.query.filter_by(role=ROLE_ADMIN).all():
        _send(
            admin.email,
            "【プレスリリースサポート】承認待ちの書類が提出されました",
            f"{release.user.company_name} から、承認待ちの書類が提出されました。\n"
            f"タイトル：{release.title}\n\n確認はこちら：\n{link}",
        )


def _changes_text(release: Release) -> str:
    """管理者が編集した変更点（要旨は管理者が書くものなので除く）。"""
    lines = []
    for edit in release.edits:
        if edit.field_name == "summary":
            continue
        label = FIELD_LABELS.get(edit.field_name, edit.field_name)
        lines.append(f"・{label}\n  変更前：{edit.old_value or '（なし）'}\n  変更後：{edit.new_value or '（なし）'}")
    return "\n".join(lines)


def notify_approved(release: Release) -> None:
    """承認されたことを、クライアントに知らせる（F23）。管理者が編集した場合は、変更点を載せる。"""
    link = external_url("releases.view", release_id=release.id)
    body = (
        f"「{release.title}」が承認されました。PDF のプレビューと修正、ダウンロードができます。\n{link}\n"
    )
    changes = _changes_text(release)
    if changes:
        body += f"\n管理者が、次の内容を修正しました。\n{changes}\n"
    _send(release.user.email, "【プレスリリースサポート】書類が承認されました", body)


def notify_rejected(release: Release) -> None:
    """差し戻したことと、その理由を、クライアントに知らせる（F23）。"""
    link = external_url("releases.edit", release_id=release.id)
    _send(
        release.user.email,
        "【プレスリリースサポート】書類が差し戻されました",
        f"「{release.title}」が差し戻されました。\n\n理由：\n{release.reject_reason}\n\n"
        f"内容を直して、もう一度提出してください。\n{link}",
    )
