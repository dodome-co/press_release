#!/usr/bin/env bash
# 更新用：最新のコードを取り込んで、アプリを再起動する。
# 使い方（アプリ用のユーザー pressrel で）：  sudo -u pressrel /opt/press-release/deploy/update.sh
set -euo pipefail
cd /opt/press-release

git fetch origin
git checkout main
git pull --ff-only origin main
/home/pressrel/.local/bin/uv sync --frozen --no-dev

set -a; source /etc/press-release.env; set +a
.venv/bin/flask db upgrade

echo "コードと、データベースの更新が終わりました。アプリを再起動します（sudo の権限で）。"
sudo systemctl restart press-release
echo "完了。状態の確認：  sudo systemctl status press-release"
