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
- `migrations/` … データベース変更の記録（Flask-Migrate）
- `app/templates/` … 画面（HTML）の雛形
- `templates/` … Word の雛形置き場（仕様書 F26。後で作成）
- `tests/` … テスト

## 本番の設定（必須）

`APP_ENV` を設定しないと本番（production）として起動し、次が揃っていないとエラーで止まる。

- `SECRET_KEY` … 推測されない長い値
- `BASE_URL` … `https://` から始まる公開 URL（メールのリンクに使う）
- `TRUSTED_HOSTS` … 公開するドメイン（カンマ区切り）。それ以外の Host は拒否される
- `MAIL_BACKEND` … 本番用のメール送信方法（未決。`console` は開発専用で、本番では使えない）
- Nginx の後ろで動かす（`PROXY_FIX=1` が本番の既定）
