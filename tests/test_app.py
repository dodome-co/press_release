def test_index_returns_200(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "プレスリリースサポート" in res.get_data(as_text=True)


def test_settings_defaults(app):
    assert app.config["FREE_RELEASE_LIMIT"] == 1
    assert app.config["INPUT_MAX_LENGTH"] == 500
    assert app.config["APPROVAL_TARGET_BUSINESS_DAYS"] == 2
