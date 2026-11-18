"""gunicorn の設定（VM でのデモ用）。systemd から、`gunicorn -c deploy/gunicorn.conf.py "app:create_app()"` で起動する。

Nginx の後ろで動かす前提：自分の PC の内側（127.0.0.1）だけで待ち受け、外には出さない。
"""
import multiprocessing
import os

bind = os.environ.get("GUNICORN_BIND", "127.0.0.1:8000")
# SQLite は、書き込みが1つずつなので、プロセスを増やしすぎない（2〜3で十分）
workers = int(os.environ.get("GUNICORN_WORKERS", min(3, max(2, multiprocessing.cpu_count()))))
threads = int(os.environ.get("GUNICORN_THREADS", 2))
timeout = 60  # Word の出力や、画像の処理に、時間がかかっても、切らない
graceful_timeout = 30
keepalive = 5
# 管理用の通信口（gunicornc 用）は使わない。systemd の ProtectHome で、ホームフォルダに作れず、ログにエラーが出るため
control_socket_disable = True
max_requests = 1000  # 長く動かしても、メモリが増え続けないよう、ときどき作り直す
max_requests_jitter = 100

# ログは、標準出力・標準エラーへ（systemd の journal に入る）
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOGLEVEL", "info")
# アクセスログには、相手の IP（Nginx が X-Forwarded-For に入れたもの）を出す
access_log_format = '%({x-forwarded-for}i)s "%(r)s" %(s)s %(b)s "%(f)s" %(L)ss'
