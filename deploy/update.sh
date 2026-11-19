#!/usr/bin/env bash
# 更新用：最新のコードを取り込み、データベースを更新して、アプリを再起動する。
# 使い方（sudo が使えるユーザーで）：  sudo /opt/press-release/deploy/update.sh
#
# コードの取り込み・パッケージ・データベースの更新は、アプリ用のユーザー（pressrel）で行う
# （ファイルの持ち主を変えないため）。再起動だけを、管理者の権限で行う。
# pressrel には sudo の権限がないので、pressrel で実行すると、最後の再起動ができない。
set -euo pipefail

APP_DIR=/opt/press-release
APP_USER=pressrel
ENV_FILE=/etc/press-release.env

if [[ $EUID -ne 0 ]]; then
  echo "sudo を付けて実行してください：  sudo $APP_DIR/deploy/update.sh" >&2
  exit 1
fi

# アプリ用のユーザーで、アプリのフォルダの中で、コマンドを実行する
as_app() {
  sudo -u "$APP_USER" -H bash -c "cd $APP_DIR && $1"
}

echo "1/4 最新のコードを取り込みます（main）"
as_app "git fetch origin && git checkout main && git pull --ff-only origin main"

echo "2/4 パッケージを入れます"
as_app "/home/$APP_USER/.local/bin/uv sync --frozen --no-dev"

echo "3/4 データベースを更新します"
as_app "set -a; source $ENV_FILE; set +a; .venv/bin/flask db upgrade"

echo "4/4 アプリを再起動します"
systemctl restart press-release

if systemctl is-active --quiet press-release; then
  echo "完了。アプリは動いています。"
else
  echo "アプリが起動していません。理由：  sudo journalctl -u press-release -n 50 --no-pager" >&2
  exit 1
fi
