from copyright_alert import oauth_setup


def test_exchange_code_uses_app_access_token_as_bearer_header(monkeypatch):
    calls = []

    def fake_get_app_access_token(app_id, app_secret):
        assert app_id == "app_id"
        assert app_secret == "app_secret"
        return "app_token_123"

    def fake_post_json(url, payload, headers=None, timeout=30):
        calls.append({"url": url, "payload": payload, "headers": headers, "timeout": timeout})
        return {"data": {"refresh_token": "refresh_123"}}

    monkeypatch.setattr(oauth_setup, "get_app_access_token", fake_get_app_access_token)
    monkeypatch.setattr(oauth_setup, "post_json", fake_post_json)

    response = oauth_setup.exchange_code("app_id", "app_secret", "auth_code_123")

    assert response == {"data": {"refresh_token": "refresh_123"}}
    assert calls == [
        {
            "url": oauth_setup.TOKEN_URL,
            "payload": {"grant_type": "authorization_code", "code": "auth_code_123"},
            "headers": {"Authorization": "Bearer app_token_123"},
            "timeout": 30,
        }
    ]
