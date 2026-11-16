"""メール送信の入口。

開発・テストでは送信せず、ログに出力する（テストでは outbox にも貯める）。
本番の送信方法は未決（仕様書 9章）のため、決まったらここを差し替える。
"""
from flask import current_app


def send_mail(to: str, subject: str, body: str) -> None:
    outbox = current_app.extensions.setdefault("mail_outbox", [])
    outbox.append({"to": to, "subject": subject, "body": body})
    current_app.logger.info("【メール（開発用・送信しない）】to=%s subject=%s\n%s", to, subject, body)
