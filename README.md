# Scrapling Service

A public, unauthenticated API for extracting values from HTTP(S) HTML pages using [Scrapling](https://github.com/D4Vinci/Scrapling). HTTP-only: JavaScript-rendered content is not supported. No IP allowlist is configured.

## API

`GET /health` → `{"status":"ok"}`. `GET /docs` provides interactive OpenAPI docs; `GET /openapi.json` provides the schema.

`POST /v1/scrape` with `Content-Type: application/json`:

```json
{"url":"https://example.com","selectors":{"title":"title::text","links":"a::attr(href)"}}
```

Returns `{"url":"https://example.com","status":200,"data":{"title":["Example Domain"],"links":[]},"provider":"scrapling"}` (extracted values depend on the target page). `provider` is either `scrapling` or `firecrawl`. Missing matches return empty arrays. URL is required; selectors are optional. Each name maps to one Scrapling CSS selector (including `::text` or `::attr(...)`). Maximum 20 selectors, 20 returned values per selector. Invalid input/destinations/selectors return 422, oversized upstream pages return 413, and upstream failures return 502.

Some sites, including Booking.com, respond to the HTTP fetcher with a JavaScript anti-bot challenge rather than the requested page. On recognized HTTP 202 challenges the API tries Firecrawl's HTML scrape endpoint if `FIRECRAWL_API_KEY` is set on the server. Firecrawl may still fail or return a challenge, in which case the API returns 502 rather than misleading empty data. Other empty matches and upstream failures do not trigger paid fallback. The public API has no authentication: **any caller can trigger billable Firecrawl requests**; add rate limits or authentication before production use. Do not commit API keys to this repository.

Firecrawl's processed HTML may omit `<head>`. If `title::text` has no match, the API uses Firecrawl's title metadata for that selector. Other selectors always extract from the returned HTML.

## Run and test

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
docker build -t scrapling-service .
docker run --rm -p 8000:8000 scrapling-service
```

## Deployment and safety

Coolify Dockerfile application, internal port 8000, health check `/health`. Only HTTP(S) ports 80/443 are supported; DNS must resolve solely to public addresses and the selected IPv4 address is pinned to curl for each hop. Redirects are checked individually (at most three). Upstream transfers have a 10-second timeout, a 1 MB body cap, and no retries; two workers and bounded concurrency cap simultaneous work. Deployment is intentionally unauthenticated and has no IP whitelist. For a production open scraping API, add network egress firewall rules and edge rate limits: application-level checks cannot guarantee protection against all network-layer abuse or excessive traffic.

Set `FIRECRAWL_API_KEY` as a **runtime-only Coolify environment variable** to enable fallback; it is never returned to callers. Firecrawl returns up to 5 MB of JSON, but extracted HTML is capped at 1 MB. The fallback uses a 30-second provider timeout and a 40-second HTTP timeout. It sends the requested URL to Firecrawl, so only use it for pages you're allowed to scrape and share with that provider.