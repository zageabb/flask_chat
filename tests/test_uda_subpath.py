"""UDA and LAN routing smoke test without network calls."""
from app import app

def test_prefix_and_lan():
    app.config["TESTING"] = True
    client = app.test_client()
    local = client.get("/")
    assert local.status_code == 200
    assert '<base href="/">' in local.get_data(as_text=True)
    headers = {"X-Forwarded-Prefix":"/apps/flask-chat",
               "X-Forwarded-Host":"tanyaanne.ddns.net",
               "X-Forwarded-Proto":"https"}
    proxied = client.get("/", headers=headers)
    assert proxied.status_code == 200
    html = proxied.get_data(as_text=True)
    assert '<base href="/apps/flask-chat/">' in html
    assert 'fetchScoped("/api/messages/clear"' in html
    assert 'new URL(path.slice(1), document.baseURI)' in html
