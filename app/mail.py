"""メール送信の入口。

- console（開発・テスト用）：送信せずに、ログに出す。
- database（デモ用）：送信せずに、データベースに記録する。管理者の画面（/admin/mails）で見られる。
- 本番用の送信方法は未決（仕様書 9章）のため、決まったらここに追加する。未実装の方法を指定すると
  エラーにして、「送ったつもりで送れていない」ことを防ぐ。

database は、呼び出し側が、自分の変更を commit したあとに呼ぶ前提（この中で commit する）。
"""
from flask import current_app


def send_mail(to: str, subject: str, body: str) -> None:
    backend = current_app.config["MAIL_BACKEND"]
    if backend == "console":
        # 開発・テスト専用。本文（再設定リンクなど）をログに出す。本番では使えない設定になっている
        current_app.extensions.setdefault("mail_outbox", []).append(
            {"to": to, "subject": subject, "body": body}
        )
        current_app.logger.info("【メール（開発用・送信しない）】to=%s subject=%s\n%s", to, subject, body)
        return
    if backend == "database":
        from .extensions import db
        from .models import MailLog

        db.session.add(MailLog(to_address=to[:255], subject=subject[:255], body=body))
        db.session.commit()
        # ログには、本文（再設定リンクなど）を出さない
        current_app.logger.info("【メール（デモ用・送信しない）を記録しました】to=%s subject=%s", to, subject)
        return
    raise NotImplementedError(f"MAIL_BACKEND={backend} の送信方法はまだ実装されていません")
