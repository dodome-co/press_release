# CLAUDE.md

プレスリリース作成支援アプリ「ゆきわり。」。クライアント（地域の中小企業）が入力した内容から、元記者の管理者が要旨を書いて承認し、プレスリリースの Word を出力する Flask アプリ。有料サービスの申込み（Stripe）もある。ビジネスプランの発表用のデモとして、Google Cloud の VM で動かしている。

- 要件：[docs/spec/requirements.md](docs/spec/requirements.md)（詳細は [docs/仕様書.md](docs/仕様書.md)）
- 画面：[docs/design/screens.md](docs/design/screens.md)
- データベース：[docs/design/database.md](docs/design/database.md)（詳細は [docs/データベース設計.md](docs/データベース設計.md)）
- VM への設置：[docs/VM設置手順.md](docs/VM設置手順.md)

## 技術スタック

| 分類 | 使っているもの |
| --- | --- |
| 言語 | Python 3.14（開発）。`pyproject.toml` の指定は 3.12 以上で、VM（Ubuntu 24.04）は 3.12 で動かす |
| パッケージ管理 | uv（`uv.lock` で固定） |
| Web | Flask 3、Jinja2（`app/templates/`）、CSS（`app/static/css/style.css`） |
| データベース | Flask-SQLAlchemy、Flask-Migrate（Alembic）。開発とテストは SQLite、VM は MySQL 8（PyMySQL、cryptography） |
| 認証・フォーム | Flask-Login、Flask-WTF（CSRF 対策）、email-validator。パスワードは Werkzeug のハッシュ |
| Word の出力 | docxtpl（python-docx）。雛形は `templates/press_release.docx` |
| 画像 | Pillow（形式・向き・大きさの検証） |
| 決済 | Stripe（Checkout と Webhook。デモはテストモードだけ） |
| 設定 | 環境変数（開発は `.env` を python-dotenv で読む。VM は `/etc/press-release.env`） |
| 公開（VM） | gunicorn、Nginx（合言葉の Basic 認証）、systemd。設定は `deploy/` |
| テスト | pytest（約800件） |

## コマンド

```bash
uv sync                      # 依存パッケージを入れる
cp .env.example .env         # 設定ファイルを作る（APP_ENV=development）
uv run flask db upgrade      # データベースを作る・更新する
uv run flask create-admin    # 管理者を作る（画面からは作れない。パスワードは対話で入力する）
uv run flask run             # 開発サーバー
uv run pytest                # 全テスト（SQLite のメモリ上。2〜3分）
uv run flask db migrate -m "説明"   # モデルを変えたあと、マイグレーションを作る
uv run flask db check        # モデルとマイグレーションのずれを確かめる
uv run flask seed-demo       # デモのサンプルデータを足す（デモ環境だけ）
uv run flask reset-demo --yes  # デモのデータを作り直す（デモ環境だけ）
```

- MySQL で全テストを通すときは、テスト専用のデータベースを `TEST_DATABASE_URL`（`.env` に書いてよい）で指定して `uv run pytest`。テストは毎回、全テーブルを作って消すので、本物のデータが入ったデータベースは指定しない。
- VM の更新は `sudo -u pressrel /opt/press-release/deploy/update.sh`（main を取り込み、`db upgrade` して、再起動する）。

## 構成

| 場所 | 中身 |
| --- | --- |
| `app/__init__.py` | アプリの組み立て（`create_app`） |
| `app/config.py` | 設定。`APP_ENV` で development / demo / production / testing を切り替える。本番とデモは、設定が足りないと起動しない |
| `app/models.py` | テーブルの定義（13個） |
| `app/auth/` | 会員登録・ログイン・パスワード再設定、アクセス制御のデコレーター（`client_required` など） |
| `app/releases/` | クライアントの書類（入力・提出）。`service.py` に状態の変更の決まりがある |
| `app/admin.py` | 管理者の書類の確認・編集・承認・差し戻し |
| `app/orders/` | サービスの申込み、Stripe の支払いと Webhook、返金 |
| `app/admin_orders.py`、`app/accounts.py`、`app/admin_services.py`、`app/admin_mails.py` | 管理者の注文・会員・自社サービス・メールの画面 |
| `app/services_catalog.py` | サービスの名前・説明・料金（ここ1か所で決める） |
| `app/docgen.py` | Word の出力 |
| `app/uploads.py`、`app/files.py` | 画像・ファイルのアップロードの検証と保存、表示 |
| `app/ratelimit.py` | 回数制限 |
| `app/mail.py` | メールの送り方（console / database） |
| `migrations/` | マイグレーション |
| `tests/` | テスト。共通の準備は `tests/conftest.py` |
| `deploy/` | VM の設定（gunicorn、systemd、Nginx、環境変数の例、更新・バックアップのスクリプト） |

## 決まり

### コード
- **設定値をコードに書き込まない。** 上限（無料の書類1件、下書き3件、タイトル22文字など）や料金は、`app/config.py` か環境変数で変えられるようにする。
- **秘密の値（キー、パスワード、個人の連絡先）は、コードにも Git にも入れない。** `.env` は Git に入れない。
- **SQLite でしか動かない書き方をしない。** 文字列の列には長さを付ける。`PRAGMA` や SQLite 専用の関数を使わない。日時の計算は Python で行う。
- **データベースの変更は、必ずマイグレーションにする。** 作ったら、空のデータベースへの `db upgrade` と、`db check` で確かめる。MySQL では、外部キーが使うインデックスを単独で消せないので、テーブルを消すときは `drop_table` だけにする。
- **日時は UTC で保存し、画面に出すときに日本時間にする。** 金額は整数（円、税込）。
- **状態や種類は文字列で保存し**、取りうる値は `app/models.py` の定数で決める。

### 安全
- ログインが必要な画面には、`client_required` / `admin_required` を付ける。
- 他人の書類や注文は、存在するかどうかも分からないように 404 を返す。下書きは、提出されるまで管理者にも見せない。
- 支払いの確定は、完了ページではなく Stripe の Webhook で行う。Webhook は署名を確かめ、同じ通知を2回処理しない（`stripe_events`）。
- アップロードは、拡張子だけでなく中身も検証する。
- 管理者は画面からは作れない（`flask create-admin` だけ）。

### テスト
- 変更したら、`uv run pytest` を通す。データベースに関わる変更は、MySQL でも通す。
- 新しい機能や直したバグには、テストを足す。認証・アクセス制御・アップロード・Webhook を優先する。

### 文章（画面・メール・ドキュメント）
- 利用者は、広報に詳しくない中小企業の人。画面の文言は、短く、やさしい日本語で書く。
- 決まっていないこと（問い合わせ先など）は、「準備中」「仮」と分かるように書く。

## 設計の要点

- **書類の流れ**：下書き（draft）→ 承認待ち（pending）→ 承認済み（approved）または差し戻し（rejected）。差し戻しから再提出できる。要旨（前文）は管理者が書き、空のままでは承認できない。**Word は承認した時点で作る**（提出時には作らない）。
- **無料枠**：初めて提出した書類だけを数える（`first_submitted_at`）。差し戻し後の再提出は数えない。枠は「リリース作成の追加」の購入で増える。
- **注文の流れ**：未払い（unpaid）→ 支払済み（paid）→ 進捗（受付→着手→納品済み）。返金の上限は進捗で決まる（受付＝全額、着手＝50%、納品済み＝不可）。
- **退会**：会員・書類・写真・ファイルを消し、注文は `user_id` を空にして、匿名で残す。
- **環境の切り替え**：`APP_ENV` で決める。デモ（`demo`）は、Stripe のテスト用キーだけを許可し、メールは送らずに `mail_logs` に記録する。設定が足りないときは、起動時に止める。
- **第2弾（まだ作らない）**：AI による要旨の文書案（`summary_source=ai_assisted`）、音声入力（`input_method=voice`）。列だけ先に用意してある。
