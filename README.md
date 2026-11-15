# プレスリリースサポートアプリ

仕様は [docs/仕様書.md](docs/仕様書.md) を参照。

## 開発の始め方

```bash
uv sync                      # 依存パッケージを入れる
cp .env.example .env         # 設定ファイルを作る（.env は Git に入れない）
uv run flask run             # 開発サーバーを起動
uv run pytest                # テストを実行
```

## 構成

- `app/` … アプリ本体（Flask）。`config.py` で開発/本番/テストの設定を切り替える
- `app/templates/` … 画面（HTML）の雛形
- `templates/` … Word の雛形置き場（仕様書 F26。後で作成）
- `tests/` … テスト
