# Scrapling Service

A public, unauthenticated API for extracting values from HTTP(S) HTML pages using [Scrapling](https://github.com/D4Vinci/Scrapling). HTTP-only: JavaScript-rendered content is not supported. No IP allowlist is configured.

## API

`GET /health` → `{"status":"ok"}`. `GET /docs` provides interactive OpenAPI docs; `GET /openapi.json` provides the schema.

`POST /v1/scrape` with `Content-Type: application/json`:

```json
{"url":"https://example.com","selectors":{"title":"title::text","links":"a::attr(href)"}}
```

Returns `{"url":"https://example.com","status":200,"data":{"title":["Example Domain"],"links":[]}}` (extracted values depend on the target page). Missing matches return empty arrays. URL is required; selectors are optional. Each name maps to one Scrapling CSS selector (including `::text` or `::attr(...)`). Maximum 20 selectors, 20 returned values per selector. Invalid input/destinations/selectors return 422, oversized upstream pages return 413, and upstream failures return 502.

Some sites, including Booking.com, respond to the HTTP fetcher with a JavaScript anti-bot challenge rather than the requested page. A recognized HTTP 202 challenge now returns 502 with an explanatory `detail` instead of misleading empty data. This service does not render JavaScript or bypass challenges; an authorized browser-rendering integration would need separate network-egress protections before it could be exposed publicly, and may still be blocked by the target site.

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