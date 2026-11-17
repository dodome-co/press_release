# プレスリリースサポートアプリ

仕様は [docs/仕様書.md](docs/仕様書.md) を参照。

## 開発の始め方

```bash
uv sync                      # 依存パッケージを入れる
cp .env.example .env         # 設定ファイルを作る（.env は Git に入れない。APP_ENV=development になっている）
uv run flask db upgrade      # データベースを作る（初回と、マイグレーション追加後）
uv run flask create-admin    # 管理者を作る（画面からは作れない。パスワードは --password で渡さず、対話で入力する）
uv run flask run             # 開発サーバーを起動
uv run pytest                # テストを実行
```

## 構成

- `app/` … アプリ本体（Flask）。`config.py` で開発/本番/テストの設定を切り替える
- `app/auth/` … 認証（会員登録・ログイン・パスワード再設定・アクセス制御）
- `app/releases/` … 書類（クライアントの入力・提出）、`app/admin.py` … 管理者の確認・編集・承認
- `app/docgen.py` … Word の出力、`app/uploads.py` … 画像のアップロード（検証・保存）
- `migrations/` … データベース変更の記録（Flask-Migrate）
- `app/templates/` … 画面（HTML）の雛形
- `templates/press_release.docx` … Word の雛形（仕様書 F26）。Word で見た目を整えてよい。作り直すときは `uv run python scripts/make_template.py`（Word で手を入れた内容は消える）
- `tests/` … テスト

## 本番の設定（必須）

`APP_ENV` を設定しないと本番（production）として起動し、次が揃っていないとエラーで止まる。

- `SECRET_KEY` … 推測されない長い値
- `BASE_URL` … `https://` から始まる公開 URL（メールのリンクに使う）
- `TRUSTED_HOSTS` … 公開するドメイン（カンマ区切り）。それ以外の Host は拒否される
- `MAIL_BACKEND` … 本番用のメール送信方法（未決。`console` は開発専用で、本番では使えない）
- Nginx の後ろで動かす（`PROXY_FIX=1` が本番の既定）

## 決済（Stripe）を試す（開発）

1. Stripe のテスト用のキー（`sk_test_...`）を `.env` の `STRIPE_SECRET_KEY` に書く（本番用のキーは使わない）。
2. [Stripe CLI](https://docs.stripe.com/stripe-cli) を入れて、ログインする。
3. 別のターミナルで、通知をローカルに転送する：
   ```bash
   stripe listen \
     --events checkout.session.completed,checkout.session.async_payment_succeeded,checkout.session.expired \
     --forward-to localhost:5000/stripe/webhook
   ```
   表示される `whsec_...` を `.env` の `STRIPE_WEBHOOK_SECRET` に書き、`flask run` を再起動する（以前のアプリのものは使い回さない）。
4. 「サービス」の画面から申し込み、テスト用のカード番号 `4242 4242 4242 4242`（期限は未来の日付、CVC は任意の3桁）で支払う。
5. 支払いが確定するのは、Stripe からの通知（Webhook）を受け取ったとき。完了ページを開いただけでは確定しない。

### 決済まわりの注意
- 開発では、本番用のキー（`sk_live_`）を `.env` に書くとアプリが起動しません（実際のお金が動かないように）。
- 本番では、本番用のキーが必須です。テスト用のキーで試すときだけ、`ALLOW_TEST_STRIPE_KEY=1` を付けます。
- **返金は、いまは Stripe の管理画面で直接行わないでください。** アプリは返金の通知を受け取っていないため、購入した書類の枠が減りません（管理者の返金機能は、次の段階で作ります）。
- 同じ注文に2回目の支払いが届いたとき（古い支払い画面から払われたとき）は、管理者にメールが届きます。Stripe の管理画面で確認して、必要なら返金してください。
