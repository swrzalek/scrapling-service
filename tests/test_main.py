import socket
import json
from io import BytesIO
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


def test_no_selectors_returns_full_page():
    def fake_fetch(url, **kwargs):
        kwargs["content_callback"](b"<html><head><title>Example</title></head><body><main><h1>Hello</h1><p>Useful words</p></main><script>ignore()</script><style>hide</style><noscript>no JS</noscript></body></html>")
        return Page()

    with patch("app.main.resolve_public", return_value=("example.com", 443, "93.184.215.14")), patch("scrapling.engines.static._SyncSessionLogic.get", side_effect=fake_fetch):
        response = client.post("/v1/scrape", json={"url": "https://example.com"})

    assert response.status_code == 200
    result = response.json()
    assert result["success"] is True
    assert result["provider"] == "scrapling"
    assert result["data"]["metadata"] == {"title": "Example", "description": None, "sourceURL": "https://example.com", "statusCode": 200}
    assert set(result["data"]) == {"markdown", "metadata"}
    assert "Hello" in result["data"]["markdown"]
    assert "Useful words" in result["data"]["markdown"]
    assert "ignore()" not in result["data"]["markdown"]


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


def test_booking_challenge_is_not_reported_as_empty_success():
    class Challenge:
        status = 202
        headers = {}

    def fake_fetch(url, **kwargs):
        kwargs["content_callback"](b"<h1>JavaScript is disabled</h1><script>function reportChallengeError(){}</script>")
        return Challenge()

    with patch("app.main.resolve_public", return_value=("www.booking.com", 443, "1.1.1.1")), patch("scrapling.engines.static._SyncSessionLogic.get", side_effect=fake_fetch), patch.dict("os.environ", {"FIRECRAWL_API_KEY": ""}):
        response = client.post("/v1/scrape", json={"url": "https://www.booking.com/", "selectors": {"title": "title::text"}})

    assert response.status_code == 502
    assert "fallback is not configured" in response.json()["detail"]


def test_challenge_falls_back_to_firecrawl_html():
    class Challenge:
        status = 202
        headers = {}

    def fake_fetch(url, **kwargs):
        kwargs["content_callback"](b"<h1>JavaScript is disabled</h1><script>challenge</script>")
        return Challenge()

    response_body = {"success": True, "data": {"html": "<title>Hotel page</title>",
                     "metadata": {"sourceURL": "https://www.booking.com/hotel/test", "statusCode": 200}}}
    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request.full_url, json.loads(request.data), timeout))
        return BytesIO(json.dumps(response_body).encode())

    with patch("app.main.resolve_public", return_value=("www.booking.com", 443, "1.1.1.1")), patch("scrapling.engines.static._SyncSessionLogic.get", side_effect=fake_fetch), patch("app.main.urlopen", side_effect=fake_urlopen), patch.dict("os.environ", {"FIRECRAWL_API_KEY": "test-secret"}):
        response = client.post("/v1/scrape", json={"url": "https://www.booking.com/", "selectors": {"title": "title::text"}})

    assert calls == [("https://api.firecrawl.dev/v2/scrape", {"url": "https://www.booking.com/", "formats": ["html"], "timeout": 30000}, 40)]
    assert response.status_code == 200
    assert response.json() == {"url": "https://www.booking.com/hotel/test", "status": 200, "data": {"title": ["Hotel page"]}, "provider": "firecrawl"}


def test_firecrawl_challenge_is_not_reported_as_success():
    from app.main import firecrawl_fallback
    from fastapi import HTTPException

    body = {"success": True, "data": {"html": "<h1>JavaScript is disabled</h1><script>challenge</script>", "metadata": {"statusCode": 202}}}
    with patch("app.main.resolve_public", return_value=("example.com", 443, "1.1.1.1")), patch("app.main.urlopen", return_value=BytesIO(json.dumps(body).encode())), patch.dict("os.environ", {"FIRECRAWL_API_KEY": "test-secret"}):
        try:
            firecrawl_fallback("https://example.com/", {"title": "title::text"})
        except HTTPException as exc:
            assert exc.status_code == 502
        else:
            assert False, "Expected an upstream challenge error"


def test_firecrawl_metadata_title_when_head_is_missing():
    from app.main import firecrawl_fallback

    body = {"success": True, "data": {"html": "<main><a href='/hotel'>Hotels</a></main>",
            "metadata": {"title": "Booking.com", "statusCode": 200}}}
    with patch("app.main.resolve_public", return_value=("example.com", 443, "1.1.1.1")), patch("app.main.urlopen", return_value=BytesIO(json.dumps(body).encode())), patch.dict("os.environ", {"FIRECRAWL_API_KEY": "test-secret"}):
        response = firecrawl_fallback("https://example.com/", {"title": "title::text", "links": "a::attr(href)"})
    assert response.data == {"title": ["Booking.com"], "links": ["/hotel"]}


def test_firecrawl_no_selectors_returns_full_page():
    from app.main import firecrawl_fallback

    body = {"success": True, "data": {"html": "<main><p>Available hotels</p></main>", "markdown": "Available hotels",
            "metadata": {"title": "Booking.com", "statusCode": 200}}}
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(json.loads(request.data))
        return BytesIO(json.dumps(body).encode())

    with patch("app.main.resolve_public", return_value=("example.com", 443, "1.1.1.1")), patch("app.main.urlopen", side_effect=fake_urlopen), patch.dict("os.environ", {"FIRECRAWL_API_KEY": "test-secret"}):
        response = firecrawl_fallback("https://example.com/", {})
    assert calls[0]["formats"] == ["markdown", "html"]
    assert response.model_dump() == {"success": True, "data": {"markdown": "Available hotels", "metadata": {"title": "Booking.com", "description": None, "sourceURL": "https://example.com/", "statusCode": 200}}, "provider": "firecrawl"}


def test_normal_accepted_response_still_extracts():
    class Accepted:
        status = 202
        headers = {}

    def fake_fetch(url, **kwargs):
        kwargs["content_callback"](b"<title>Queued</title>")
        return Accepted()

    with patch("app.main.resolve_public", return_value=("example.com", 443, "1.1.1.1")), patch("scrapling.engines.static._SyncSessionLogic.get", side_effect=fake_fetch):
        response = client.post("/v1/scrape", json={"url": "https://example.com/", "selectors": {"title": "title::text"}})

    assert response.status_code == 200
    assert response.json()["data"]["title"] == ["Queued"]