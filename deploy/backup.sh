#!/usr/bin/env bash
# バックアップ：データベースと、アップロードしたファイルを、日付つきで保存する（7日分だけ残す）。
# 使い方：  sudo /opt/press-release/deploy/backup.sh      cron で、毎日動かすこともできる（手順書を参照）
# データベースが SQLite か MySQL かは、/etc/press-release.env の DATABASE_URL で判断する。
set -euo pipefail

DATA=/var/lib/press-release
DEST=/var/backups/press-release
STAMP=$(date +%Y%m%d-%H%M%S)
ENV_FILE=/etc/press-release.env

mkdir -p "$DEST"
URL=$(grep -E '^DATABASE_URL=' "$ENV_FILE" | tail -n 1 | cut -d= -f2-)

if [[ "$URL" == mysql* ]]; then
  # mysql+pymysql://ユーザー:パスワード@ホスト:ポート/データベース名?charset=utf8mb4 を分解する
  REST=${URL#*://}; CRED=${REST%%@*}; HOSTDB=${REST#*@}
  DBUSER=${CRED%%:*}; DBPASS=${CRED#*:}
  HOSTPORT=${HOSTDB%%/*}; DBNAME=${HOSTDB#*/}; DBNAME=${DBNAME%%\?*}
  DBHOST=${HOSTPORT%%:*}; DBPORT=3306; [[ "$HOSTPORT" == *:* ]] && DBPORT=${HOSTPORT##*:}
  # パスワードを、コマンドの引数（ps で見える）に出さず、一時ファイルで渡す
  CNF=$(mktemp); trap 'rm -f "$CNF"' EXIT; chmod 600 "$CNF"
  printf '[client]\nuser=%s\npassword=%s\nhost=%s\nport=%s\n' "$DBUSER" "$DBPASS" "$DBHOST" "$DBPORT" > "$CNF"
  # --single-transaction：動いている最中でも、整合性のあるコピーになる
  mysqldump --defaults-extra-file="$CNF" --single-transaction --no-tablespaces --default-character-set=utf8mb4 "$DBNAME" | gzip > "$DEST/app-$STAMP.sql.gz"
  DBFILE="app-$STAMP.sql.gz"
else
  # SQLite は、動いている最中に、ファイルを単にコピーすると、壊れたコピーになることがある。専用の方法で、整合性のあるコピーを作る
  sqlite3 "$DATA/app.sqlite3" ".backup '$DEST/app-$STAMP.sqlite3'"
  DBFILE="app-$STAMP.sqlite3"
fi
tar -czf "$DEST/uploads-$STAMP.tar.gz" -C "$DATA" uploads 2>/dev/null || true
chmod 600 "$DEST"/*
# 7日より古いものは、消す
find "$DEST" -type f -mtime +7 -delete
echo "バックアップしました：$DEST（$DBFILE / uploads-$STAMP.tar.gz）"
