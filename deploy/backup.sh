#!/usr/bin/env bash
# バックアップ：データベースと、アップロードしたファイルを、日付つきで保存する（7日分だけ残す）。
# 使い方：  sudo /opt/press-release/deploy/backup.sh      cron で、毎日動かすこともできる（手順書を参照）
set -euo pipefail

DATA=/var/lib/press-release
DEST=/var/backups/press-release
STAMP=$(date +%Y%m%d-%H%M%S)

mkdir -p "$DEST"
# SQLite は、動いている最中に、ファイルを単にコピーすると、壊れたコピーになることがある。専用の方法で、整合性のあるコピーを作る
sqlite3 "$DATA/app.sqlite3" ".backup '$DEST/app-$STAMP.sqlite3'"
tar -czf "$DEST/uploads-$STAMP.tar.gz" -C "$DATA" uploads 2>/dev/null || true
chmod 600 "$DEST"/*
# 7日より古いものは、消す
find "$DEST" -type f -mtime +7 -delete
echo "バックアップしました：$DEST（app-$STAMP.sqlite3 / uploads-$STAMP.tar.gz）"
