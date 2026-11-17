# VM への設置手順（ビジネスプラン発表用のデモ）

Google Cloud の VM（Compute Engine）に、アプリを置いて、発表の相手が、自分の端末から触れるようにする手順。

- **前提**：Ubuntu 24.04 LTS、データベースは VM の中の SQLite、HTTP（鍵マークなし）、IP アドレスで公開、Nginx の合言葉つき。
- 別の OS や構成にするときは、読み替える（コマンドや、パッケージの名前が変わる）。
- 設定ファイルの雛形は、リポジトリの `deploy/` にある。

## 0. 全体の流れ
1. VM を作る（Google Cloud のコンソール）
2. VM の準備（パッケージ、ユーザー、フォルダ）
3. アプリを置く
4. 設定ファイルを作る
5. データベースを作り、サンプルデータを入れる
6. アプリの自動起動（systemd）
7. Nginx（公開・合言葉）
8. Stripe の Webhook を登録する
9. 動作の確認
10. 発表のあと：VM を止める

## 1. VM を作る（Google Cloud のコンソール）
- 場所：Compute Engine →「VM インスタンス」→「インスタンスを作成」。
- **リージョン**：`asia-northeast1`（東京）。
- **マシンタイプ**：`e2-small`（2 vCPU・2GB メモリ）が目安。発表で、同時に触る人が数人なら、足りる。料金は、Google Cloud の料金表で確認する（動かしている間だけ、かかる）。
- **ブートディスク**：Ubuntu 24.04 LTS、標準の永続ディスク 20GB。
- **ファイアウォール**：「HTTP トラフィックを許可する」にチェック（ポート80）。HTTPS は、使わない。
- **外部 IP アドレス**：「固定の外部 IP」を予約して、この VM に付ける（VPC ネットワーク →「IP アドレス」→「外部静的 IP アドレスを予約」）。固定にしないと、VM を止めて起動し直すたびに、IP が変わり、URL と Stripe の設定を、直すことになる。**固定の IP は、VM に付けていない間も、料金がかかる**ので、発表のあとは、解放する。
- 作ったら、VM の一覧の「SSH」ボタンで、ブラウザから接続できる。以降のコマンドは、この画面で実行する。
- 控えるもの：**外部 IP アドレス**（以降、`203.0.113.10` の例で書く。自分の IP に読み替える）。

## 2. VM の準備
```bash
sudo apt update && sudo apt -y upgrade
sudo apt -y install nginx git sqlite3 apache2-utils curl

# アプリ用のユーザー（ログインできない。アプリを動かすためだけ）
sudo adduser --system --group --home /home/pressrel --shell /bin/bash pressrel

# データとアップロードの置き場所
sudo mkdir -p /var/lib/press-release/uploads
sudo chown -R pressrel:pressrel /var/lib/press-release
sudo chmod 750 /var/lib/press-release

# アプリの置き場所
sudo mkdir -p /opt/press-release
sudo chown pressrel:pressrel /opt/press-release
```

## 3. アプリを置く
```bash
# uv（Python のパッケージ管理）を、アプリ用のユーザーに入れる
sudo -u pressrel bash -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'

# コードを取得（GitHub のリポジトリの URL に読み替える。非公開のリポジトリなら、認証が要る）
sudo -u pressrel git clone https://github.com/dodome-co/press_release.git /opt/press-release
# すでにあるフォルダに入れるときは、 git -C /opt/press-release pull

# 必要なパッケージを入れる（開発用のものは入れない）
cd /opt/press-release
sudo -u pressrel /home/pressrel/.local/bin/uv sync --frozen --no-dev
```
- 非公開のリポジトリの場合は、デプロイキー（読み取り専用の SSH キー）か、アクセストークンを使う。トークンは、人に見せない。

## 4. 設定ファイルを作る（秘密の設定）
```bash
sudo cp /opt/press-release/deploy/env.demo.example /etc/press-release.env
sudo chown root:root /etc/press-release.env
sudo chmod 600 /etc/press-release.env
sudo nano /etc/press-release.env      # 中身を、自分のものに書き換える
```
書き換えるもの：
- `SECRET_KEY`：`python3 -c "import secrets; print(secrets.token_urlsafe(48))"` で作った、長いランダムな文字列。
- `BASE_URL` と `TRUSTED_HOSTS`：VM の外部 IP アドレス（`http://203.0.113.10` と `203.0.113.10`）。
- `STRIPE_SECRET_KEY`：Stripe の**テスト用**のキー（`sk_test_` で始まるもの）。**本番用（`sk_live_`）だと、アプリは起動しない**（実際のお金が動かないための確認）。
- `STRIPE_WEBHOOK_SECRET`：手順8で、Stripe に Webhook を登録したあとに、表示される値。いまは、仮の値でよい（あとで書き換えて、再起動する）。
- `OPERATOR_NAME`・`COMPANY_ADDRESS`・`SUPPORT_EMAIL`：About Us に載せる情報。
- **この設定ファイルは、人に見せない・Git に入れない・チャットに貼らない。**

## 5. データベースを作り、サンプルデータを入れる
```bash
cd /opt/press-release
sudo -u pressrel bash -c 'set -a; source /etc/press-release.env; set +a; .venv/bin/flask db upgrade'
sudo -u pressrel bash -c 'set -a; source /etc/press-release.env; set +a; .venv/bin/flask seed-demo'
```
- `seed-demo` が、デモ用の管理者とクライアント3社を作り、ログインできるアカウントと、共通のパスワードを表示する。控えておく。
- 自分専用の管理者を作るなら、`flask create-admin`（同じように環境変数を読み込んで実行）。

## 6. アプリの自動起動（systemd）
```bash
sudo cp /opt/press-release/deploy/press-release.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now press-release
sudo systemctl status press-release        # 「active (running)」と出ればよい
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/     # 200 が出ればよい（VM の中から）
```
- ログを見る：`sudo journalctl -u press-release -f`（止めるときは Ctrl+C）。
- 起動に失敗するときは、`journalctl -u press-release -n 50` に、理由が出る（設定の足りないもの、など）。

## 7. Nginx（公開・合言葉）
```bash
# 合言葉（Basic 認証）。発表の相手に、教える、共通のユーザー名とパスワード
sudo htpasswd -c /etc/nginx/press-release.htpasswd demo     # パスワードを聞かれる（-c は、最初の1回だけ）
sudo chown root:www-data /etc/nginx/press-release.htpasswd
sudo chmod 640 /etc/nginx/press-release.htpasswd

# 設定ファイル
sudo cp /opt/press-release/deploy/nginx-proxy-snippet.conf /etc/nginx/snippets/press-release-proxy.conf
sudo cp /opt/press-release/deploy/nginx-press-release.conf /etc/nginx/sites-available/press-release
sudo ln -sf /etc/nginx/sites-available/press-release /etc/nginx/sites-enabled/press-release
sudo rm -f /etc/nginx/sites-enabled/default

# 静的ファイル（CSS・ロゴ）を、Nginx が読めるようにする
sudo chmod o+x /opt /opt/press-release /opt/press-release/app
sudo chmod -R o+rX /opt/press-release/app/static

sudo nginx -t                  # 「syntax is ok」「test is successful」と出ればよい
sudo systemctl reload nginx
```
確認（自分のパソコンから）：
- ブラウザで `http://203.0.113.10/` を開くと、合言葉を聞かれる。入力すると、トップページが出る。
- 合言葉なしで、`curl -I http://203.0.113.10/` を実行すると、`401` が返る。

**注意（HTTP のため）**：HTTPS ではないので、合言葉も、ログインのパスワードも、通信の途中で、見ようと思えば見られる。**本物のパスワードや個人情報を、入力しないよう、発表の相手に伝える**（画面の上部にも表示している）。

## 8. Stripe の Webhook を登録する（テストモード）
支払いが確定するのは、Stripe からの通知（Webhook）を受け取ったとき。
1. Stripe の管理画面（**テストモード／サンドボックス**）→「開発者」→「Webhook」→「エンドポイントを追加」。
2. エンドポイントの URL：`http://203.0.113.10/stripe/webhook`（このパスは、合言葉の対象外にしてある）。
3. 受け取るイベント：`checkout.session.completed`、`checkout.session.async_payment_succeeded`、`checkout.session.expired`、`charge.refunded`、`charge.dispute.created`、`refund.updated`、`refund.failed`。
4. 作成すると表示される「署名シークレット（`whsec_…`）」を、`/etc/press-release.env` の `STRIPE_WEBHOOK_SECRET` に書き、再起動する：`sudo systemctl restart press-release`。
- **もし、HTTP の URL を、Stripe が受け付けない場合**：HTTPS が必要になる。ドメインなしで HTTPS にする方法として、`203-0-113-10.sslip.io`（IP を含む無料の名前）と、無料の証明書（Let's Encrypt の `certbot`）を使える。そのときは、`BASE_URL` を `https://…`、`TRUSTED_HOSTS` を、その名前にし、Cookie の Secure を有効にする設定（`DemoConfig` の `SESSION_COOKIE_SECURE`）への変更が必要になるので、相談する。
- 以前のアプリで、同じ Stripe のアカウントに、Webhook を登録している場合は、通知が、そちらにも飛ぶ。使わないものは、無効にする。

## 9. 動作の確認（発表の前に）
- [ ] `http://（IP）/` が、合言葉を聞いてから、トップページ（ロゴ・紹介文）を表示する。
- [ ] 画面の上部に、「ビジネスプランの発表用のデモです…」が出ている。
- [ ] サンプルのアカウントで、ログインできる（クライアント、管理者）。
- [ ] 管理者で、承認待ちの書類を、承認でき、Word をダウンロードできる。
- [ ] 「送信予定のメール」（管理者のメニュー）に、メールが記録される。
- [ ] テスト用のカード（`4242 4242 4242 4242`）で、「記事制作」などを申し込み、支払いが確定する（注文が「お支払い済み」になる）。
- [ ] 管理者から、返金できる。
- [ ] 画像（写真・ロゴ）のアップロードと、記事（docx・pdf）の納品ができる（Nginx の `client_max_body_size` は、12MB）。
- [ ] 発表の直前に、データを元に戻す：`flask reset-demo --yes`（管理者以外の会員・書類・注文・メール・ファイルを消して、サンプルを作り直す）。

## 10. 日々の運用
- **更新**（コードを、新しくしたとき）：`sudo -u pressrel /opt/press-release/deploy/update.sh`（最新を取り込み、データベースを更新し、再起動する。再起動には、`sudo` の権限が要る）。
- **バックアップ**：`sudo /opt/press-release/deploy/backup.sh`（データベースとアップロードを、`/var/backups/press-release` に保存。7日分）。毎日動かすなら、`sudo crontab -e` に `0 3 * * * /opt/press-release/deploy/backup.sh` を書く。
- **デモのデータを戻す**：`flask reset-demo --yes`（手順5と同じ環境変数の読み込みで実行）。
- **ログ**：アプリは `sudo journalctl -u press-release`、Nginx は `/var/log/nginx/`。
- **止める・起動する**：`sudo systemctl stop/start press-release`、`sudo systemctl reload nginx`。

## 11. 発表のあと
- **VM を止める**（コンソールで「停止」）：止めている間は、VM の料金が、かからない（ディスクの料金は、かかる）。
- 使わなくなったら、**VM を削除**し、**固定の外部 IP を解放**する（付けないまま残すと、料金がかかる）。
- Stripe の Webhook も、無効にするか、削除する。
- `/etc/press-release.env` の秘密（SECRET_KEY、Stripe のキー）は、VM と一緒に消える。Stripe のテスト用のキーを、再発行（ロール）しておくと、さらに安全。

## 困ったとき
| 症状 | 見るところ |
| --- | --- |
| アプリが起動しない | `sudo journalctl -u press-release -n 50`。`デモの設定が足りません：…` と出たら、`/etc/press-release.env` の不足を直す |
| ページが開かない（502） | `sudo systemctl status press-release`。アプリが止まっている |
| 「400 Bad Request」 | `TRUSTED_HOSTS` に、アクセスしている IP が入っていない。IP が変わっていないかも確認 |
| 合言葉を何度入れても、入れない | `/etc/nginx/press-release.htpasswd` の作り直し（`sudo htpasswd /etc/nginx/press-release.htpasswd demo`） |
| 全員が、まとめて、ログインできなくなる | Nginx が、`X-Forwarded-For` を、渡していない（`nginx-proxy-snippet.conf` を、置き直したか） |
| 写真や記事がアップロードできない | Nginx の `client_max_body_size`（12m）。`/var/lib/press-release/uploads` の書き込み権限（`pressrel`） |
| 支払いが「お支払い前」のまま | Stripe の Webhook が、届いていない。URL・署名シークレット・イベントの一覧を確認 |
