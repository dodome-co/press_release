"""メール送信の入口。

console（開発・テスト用）は送信せずログに出す。本番用の送信方法は未決
（仕様書 9章）のため、決まったらここに追加する。未実装の方法を指定すると
エラーにして、「送ったつもりで送れていない」ことを防ぐ。
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
    raise NotImplementedError(f"MAIL_BACKEND={backend} の送信方法はまだ実装されていません")
