import socket
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


class Page:
    status = 200
    headers = {}


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_rejects_private_and_invalid_urls():
    for url in ("http://127.0.0.1/", "http://169.254.169.254/", "file:///etc/passwd", "http://localhost/"):
        assert client.post("/v1/scrape", json={"url": url}).status_code == 422


def test_rejects_mixed_dns_results():
    records = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 443)),
               (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
    with patch("app.main.socket.getaddrinfo", return_value=records):
        assert client.post("/v1/scrape", json={"url": "https://example.com"}).status_code == 422


def test_extracts_with_scrapling_selectors():
    def fake_fetch(url, **kwargs):
        kwargs["content_callback"](b"<title>Example</title><a href='/item'>Item</a>")
        return Page()

    with patch("app.main.resolve_public", return_value=("example.com", 443, "93.184.215.14")), patch("scrapling.engines.static._SyncSessionLogic.get", side_effect=fake_fetch):
        response = client.post("/v1/scrape", json={"url": "https://example.com", "selectors": {"title": "title::text", "links": "a::attr(href)"}})
    assert response.status_code == 200
    assert response.json()["data"] == {"title": ["Example"], "links": ["/item"]}


def test_body_size_limit():
    def fake_fetch(url, **kwargs):
        kwargs["content_callback"](b"x" * 1_000_001)
        raise RuntimeError("Transfer aborted")

    with patch("app.main.resolve_public", return_value=("example.com", 443, "93.184.215.14")), patch("scrapling.engines.static._SyncSessionLogic.get", side_effect=fake_fetch):
        response = client.post("/v1/scrape", json={"url": "https://example.com"})
    assert response.status_code == 413


def test_redirect_rechecks_destination():
    class Redirect:
        status = 302
        headers = {"location": "http://127.0.0.1/"}

    real_resolver = __import__("app.main", fromlist=["resolve_public"]).resolve_public

    def check(url):
        if url == "https://example.com":
            return "example.com", 443, "93.184.215.14"
        return real_resolver(url)

    with patch("app.main.resolve_public", side_effect=check), patch("scrapling.engines.static._SyncSessionLogic.get", return_value=Redirect()):
        response = client.post("/v1/scrape", json={"url": "https://example.com"})
    assert response.status_code == 422