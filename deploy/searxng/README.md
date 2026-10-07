# SearXNG for udr

Optional. With `UDR_SEARXNG_URL=http://127.0.0.1:8888` the web search chain is SearXNG, then
Tavily, then ddgs; without it, Tavily then ddgs. SearXNG (AGPL-3.0) runs as an unmodified
container and is only reached over HTTP (AGENTS.md §5.5).

| Task | Command |
|---|---|
| Start | `SEARXNG_SECRET=$(openssl rand -hex 32) docker compose -f deploy/searxng/compose.yaml up -d` |
| Check | `curl -s 'http://127.0.0.1:8888/search?q=test&format=json' \| head -c 300` |
| Stop | `docker compose -f deploy/searxng/compose.yaml down` |
| Pin the image | `docker inspect --format '{{index .RepoDigests 0}}' searxng/searxng:latest`, then put that `name@sha256:...` in `compose.yaml` |

- The port is bound to `127.0.0.1` only. Keep the same `SEARXNG_SECRET` between restarts (for
  example in `.env`, which is gitignored; never commit it).
- A JSON answer of 403 means `formats` in `settings.yml` lacks `json`.
- Engines that block automated requests give 0 hits or errors; udr then falls through to Tavily
  and ddgs (event `search_fallback`). Change engines in `settings.yml` only.
