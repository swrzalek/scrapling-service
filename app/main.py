"""Public, bounded HTML extraction with Scrapling's HTTP fetcher."""

import ipaddress
import json
import os
import socket
from urllib.parse import urljoin, urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from curl_cffi import CurlOpt
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from scrapling import Selector
from scrapling.fetchers import FetcherSession

MAX_BYTES = 1_000_000
MAX_REDIRECTS = 3
MAX_VALUES = 20
MAX_TEXT_LENGTH = 10_000
FIRECRAWL_URL = "https://api.firecrawl.dev/v2/scrape"

app = FastAPI(title="Scrapling Service", version="1.0.0")


class ScrapeRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    selectors: dict[str, str] = Field(default_factory=dict, max_length=20)


class ScrapeResponse(BaseModel):
    url: str
    status: int
    data: dict[str, list[str]]
    provider: str = "scrapling"


def resolve_public(url: str) -> tuple[str, int, str]:
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("Only absolute HTTP(S) URLs are allowed")
        if parts.username or parts.password or parts.fragment:
            raise ValueError("Credentials and fragments are not allowed")
        host = parts.hostname
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if port not in (80, 443):
            raise ValueError("Only ports 80 and 443 are allowed")
        addresses = {ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
        if not addresses or any(not addr.is_global for addr in addresses):
            raise ValueError("Destination must resolve exclusively to public addresses")
        ipv4 = sorted((addr for addr in addresses if addr.version == 4), key=str)
        if not ipv4:
            raise ValueError("Destination must have a public IPv4 address")
        return host, port, str(ipv4[0])
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid destination: {exc}") from exc


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def extract(html: bytes, url: str, status: int, selectors: dict[str, str], provider: str) -> ScrapeResponse:
    try:
        parsed = Selector(content=html, url=url)
        if selectors:
            data = {name: parsed.css(css).getall()[:MAX_VALUES] for name, css in selectors.items()}
        else:
            titles = parsed.css("title::text").getall()
            fragments = parsed.css("body *:not(script):not(style):not(noscript)::text").getall()
            text = " ".join(" ".join(fragments).split())[:MAX_TEXT_LENGTH]
            data = {"title": titles[:1], "text": [text] if text else []}
    except (ValueError, TypeError, SyntaxError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid CSS selector: {exc}") from exc
    return ScrapeResponse(url=url, status=status, data=data, provider=provider)


def firecrawl_fallback(url: str, selectors: dict[str, str]) -> ScrapeResponse:
    key = os.environ.get("FIRECRAWL_API_KEY")
    if not key:
        raise HTTPException(status_code=502, detail="Upstream returned an anti-bot JavaScript challenge; Firecrawl fallback is not configured")
    # Never let a request choose the API endpoint, token, or provider options.
    payload = json.dumps({"url": url, "formats": ["html"], "timeout": 30000}).encode()
    request = Request(FIRECRAWL_URL, data=payload, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"
    }, method="POST")
    try:
        with urlopen(request, timeout=40) as response:
            raw = response.read(5_000_001)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise HTTPException(status_code=502, detail="Firecrawl fallback request failed") from exc
    if len(raw) > 5_000_000:
        raise HTTPException(status_code=502, detail="Firecrawl response exceeds 5 MB")
    try:
        result = json.loads(raw)
        data = result["data"]
        html = data["html"]
        metadata = data.get("metadata") or {}
        final_url = metadata.get("sourceURL") or url
        status = metadata.get("statusCode", 200)
        if result.get("success") is not True or not isinstance(html, str) or not html or not isinstance(final_url, str) or not isinstance(status, int):
            raise ValueError("Missing scrape content")
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=502, detail="Firecrawl returned no usable HTML") from exc
    resolve_public(final_url)
    if status >= 400 or (status == 202 and "JavaScript is disabled" in html and "challenge" in html.lower()):
        raise HTTPException(status_code=502, detail="Firecrawl returned an upstream error or challenge, not the requested page")
    if len(html.encode()) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="Firecrawl HTML exceeds 1 MB")
    extracted = extract(html.encode(), final_url, status, selectors, "firecrawl")
    # Firecrawl's processed HTML can omit <head>; its metadata preserves the title.
    if isinstance(metadata.get("title"), str) and not extracted.data.get("title"):
        if not selectors or selectors.get("title") == "title::text":
            extracted.data["title"] = [metadata["title"]]
    return extracted


@app.post("/v1/scrape", response_model=ScrapeResponse)
def scrape(request: ScrapeRequest) -> ScrapeResponse:
    if any(not name or len(name) > 64 or not css or len(css) > 256 for name, css in request.selectors.items()):
        raise HTTPException(status_code=422, detail="Selector names must be 1-64 characters and CSS selectors 1-256 characters")

    current = request.url
    for hop in range(MAX_REDIRECTS + 1):
        host, port, address = resolve_public(current)
        body = bytearray()
        too_large = False

        def receive(chunk: bytes) -> int:
            nonlocal too_large
            if len(body) + len(chunk) > MAX_BYTES:
                too_large = True
                return 0  # Abort transfer before buffering an oversized response.
            body.extend(chunk)
            return len(chunk)

        try:
            with FetcherSession() as session:
                # curl_cffi only accepts curl_options on a session, not a per-request call.
                session._curl_session.curl_options = {CurlOpt.RESOLVE: [f"{host}:{port}:{address}"]}
                page = session.get(
                    current,
                    timeout=10,
                    retries=1,
                    follow_redirects=False,
                    impersonate=None,
                    stealthy_headers=False,
                    content_callback=receive,
                )
        except Exception as exc:
            if too_large:
                raise HTTPException(status_code=413, detail="Upstream response exceeds 1 MB") from exc
            raise HTTPException(status_code=502, detail="Could not fetch upstream URL") from exc

        if page.status in (301, 302, 303, 307, 308) and page.headers.get("location"):
            if hop == MAX_REDIRECTS:
                raise HTTPException(status_code=502, detail="Too many upstream redirects")
            current = urljoin(current, page.headers["location"])
            continue

        if page.status >= 400:
            raise HTTPException(status_code=502, detail=f"Upstream returned HTTP {page.status}")
        if page.status == 202 and b"JavaScript is disabled" in body and b"challenge" in body.lower():
            return firecrawl_fallback(current, request.selectors)
        return extract(bytes(body), current, page.status, request.selectors, "scrapling")

    raise HTTPException(status_code=502, detail="Too many upstream redirects")