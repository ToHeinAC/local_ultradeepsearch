# Outbound gateway

The single door to the internet. Requirements: [PRD.md](../PRD.md) §3.2 (confidentiality) and §3.3
(search, fetch, budget). Code: `src/app/adapters/outbound/`. Only this package and the loopback-only
Ollama transport may import network modules; `tests/test_egress_guard.py` enforces it.

## Modules

| Module | Holds |
|---|---|
| `gateway.py` | `OutboundGateway` (one per run), `PreparedQuery`, `Document`, `FetchFailure`, `Providers` |
| `denylist.py` | `Denylist` matcher and file store |
| `guard.py` | `check_url`: may this URL be requested from outside? |
| `sanitizer.py` | `Sanitizer`: the `reason` model strips confidential details from a query |
| `log.py` | `OutboundLog`: one JSON line per attempt and per block |
| `ledger.py` | Tavily prices, `RunLedger`, `MonthLedger` |
| `throttle.py` | `HostThrottle`: minimum interval per host |
| `tavily.py`, `ddgs_search.py`, `scholarly.py`, `http_get.py` | One-attempt clients; raise the errors in `errors.py` |
| `extract.py` | Content-type sniffing, charset decoding, trafilatura (HTML), pypdfium2 (PDF) |
| `http_util.py`, `types.py`, `errors.py` | Shared HTTP call and status mapping, data types, errors |

`app.bootstrap.build_gateway(rt, run_dir, credit_cap=…, confidential_context=…)` builds a gateway.
It writes `<run_dir>/outbound.jsonl` and shares `data/denylist.txt` and `data/tavily-ledger.json`.

## Public calls

| Call | Does | Fails with |
|---|---|---|
| `prepare_query(query, step)` | Collapses whitespace, then denylist → sanitizer → denylist. Nothing leaves the machine. | `DenylistBlocked`, `OutboundBlocked("sanitizer_failed")` |
| `search_web(prepared, step, include_domains=(), max_results=10)` | Tavily while usable, else ddgs | `DenylistBlocked`, `SearchUnavailable` |
| `search_scholarly(prepared, step, source)` | One of `openalex`, `crossref`, `arxiv`; no credits | `DenylistBlocked`, `SearchUnavailable` |
| `fetch(url, step)` | Local fetch, then Tavily Extract as a fallback | Returns a `Document` or a `FetchFailure(reason)`; never raises |

Every send re-checks the exact payload against the denylist: the query, each `include_domains`
entry, the URL and every redirect target. That matters because callers may build a
`PreparedQuery` themselves, for example from an edited and approved search plan.

**`FetchFailure` reasons:**
- blocked before sending: `blocked_denylist`, `blocked_private` (the guard's detailed reason is in
  the log);
- the response: `too_large`, `unsupported_type`, `http_<status>`;
- the connection: `timeout`, `network`, `too_many_redirects`;
- the content: `empty_text`, `extract_failed`.

## Denylist

- **File:** `data/denylist.txt`, one term per line; `#` starts a comment.
- **Managed with:** `udr denylist add|remove|list`. A term must contain a letter or digit.
- **Matching:** both the term and the text go through NFKC normalisation and casefolding, and are
  then folded two ways: umlauts to `ae/oe/ue`, and all diacritics stripped. Both are split into
  words of letters and digits. A term matches when its words, concatenated, equal the concatenated
  words of a contiguous run of text words.
  - So `Müller-Werke` matches `MUELLER WERKE`, `Muller_Werke` and `muellerwerke`.
  - `AG` never matches inside `Tagung`.
  - Percent-encoded text is also checked decoded.
- **Limits:** a term entered without its umlaut (`Muller`) does not match the `ue` spelling
  (`Mueller`). Homoglyphs from other scripts are not folded. The denylist is the hard guarantee;
  the sanitizer is best effort.

## Sanitizer

The `reason` model (`think=False`) receives the run's confidential context and the query, using
the prompts in `app/prompts/outbound.py`. It returns `{sanitized_query, removed_terms}`, cached
per query.

It **fails closed**: any LLM error, or an empty result, blocks the query. The sanitizer's output
is checked against the denylist again (PRD M2 AC2).

## Private-URL guard

`check_url` returns `None` or a reason code:

| Reason | When |
|---|---|
| `scheme` | Not `http` or `https` |
| `no_host` / `unparseable` | No host, or the URL cannot be parsed |
| `private_ip` | An IP literal that is not globally routable: private, loopback, link-local, CGNAT, unspecified; IPv4-mapped addresses are unwrapped first |
| `single_label` | No dot in the host (`intranet`, `localhost`, `2130706433`) |
| `internal_domain` | Ends in a built-in special-use suffix (`localhost`, `local`, `localdomain`, `internal`, `intranet`, `lan`, `home.arpa`, `corp`) or in `UDR_INTERNAL_DOMAINS` |
| `resolves_private` | Any address the name resolves to is not global (catches `127.1`, `0x7f.1`, and public names pointing into the LAN) |
| `dns_failed` | The name does not resolve |

It runs before the first request and again on every redirect hop (at most 5 hops, followed by
hand).

**Known limit:** the name is resolved again when the connection is made, so a DNS answer that
changes in between (DNS rebinding) is not caught.

## Web search and provider switching

**Tavily** is used while all of these hold:
- a key is configured;
- the run has not switched;
- the month ledger is below its limit;
- the run ledger can afford one search credit.

Otherwise the run switches to **ddgs**. The switch is permanent for the run and emits
`provider_switched {from: tavily, to: ddgs, reason}` once.

| Reason | Trigger |
|---|---|
| `no_api_key` | `TAVILY_API_KEY` not set |
| `month_limit` | `data/tavily-ledger.json` at or above `UDR_TAVILY_MONTHLY_LIMIT` |
| `run_cap` | The run's credit cap is spent |
| `plan_limit` | Tavily answered HTTP 432 or 433 |
| `auth` | Tavily answered HTTP 401 or 403 |

A Tavily outage (timeouts or 5xx after the retries) or a 400 does not switch. Only that query goes
to ddgs.

## Fetch

1. Denylist check and guard on the URL.
2. Local GET:
   - no automatic redirects;
   - streamed with caps of 10 MiB for HTML and 25 MiB for PDF (`UDR_MAX_HTML_MB`,
     `UDR_MAX_PDF_MB`); a larger declared `Content-Length` is refused unread;
   - User-Agent `local-ultradeepsearch/<version> (research tool)`, with no URL and no e-mail.
3. Classification by content type, `%PDF` magic, `.pdf` path or sniffing.
   - HTML: decoded with the header charset, else the `<meta>` charset, else a detected one, then
     extracted with trafilatura to markdown (tables kept).
   - PDF: text per page with pypdfium2; no OCR.
   - Plain text and markdown: decoded as they are.
4. HTML under 300 characters of extracted text counts as `empty_text`.
5. **Tavily Extract fallback**, only for `timeout`, `network`, `http_<status>` and `empty_text`,
   and only while Tavily is usable (see above). It costs `ceil(successful / 5)` credits; URLs
   Tavily cannot extract cost nothing and return the local failure.

## Retries, pacing, credits

| Provider | Attempts | Waits | Credits |
|---|---|---|---|
| Tavily search / extract | 3 | 1 s, 2 s | 1 per search; `ceil(n/5)` per extract call |
| ddgs | 3 | 2 s, 5 s | – |
| OpenAlex, Crossref, arXiv | 3 | 1 s, 2 s | – |
| Local GET | 1 | – | – |

- **Transient** (retried): timeouts, connection errors, 429 and 5xx. Other 4xx answers are not
  retried.
- **Pacing:** 1 s between requests to the same host. `export.arxiv.org` gets 3 s (arXiv terms);
  OpenAlex and Crossref 0.1 s; Tavily none.
- **Ledgers:**
  - `RunLedger` enforces the run's cap.
  - `MonthLedger` keeps `{month, credits}` under an `fcntl` lock shared by all processes, resets
    in a new calendar month, and emits `tavily_month_warning` when a charge crosses 80 %.

## Outbound log

`<run_dir>/outbound.jsonl` gets one line per network attempt, one per redirect hop and one per
blocked attempt. The fields are always present:

```
ts, step, provider, original_query, sent_query, removed_terms, url, status, credits
```

| Field | Values |
|---|---|
| `provider` | `tavily_search`, `tavily_extract`, `ddgs_search`, `openalex`, `crossref`, `arxiv`, `http_get`; `sanitizer` / `web_search` for blocks |
| `status` | the HTTP code, `ok`, `blocked:<reason>` or `error:<timeout\|network\|transient\|provider>` |
| `url` | the request URL; for `tavily_extract`, the page sent to Tavily |

`original_query` never leaves the machine; it is in the log for the GUI.

## Settings

| Variable | Default | |
|---|---|---|
| `TAVILY_API_KEY` | – | Secret. Without it, search uses ddgs. |
| `OPENALEX_MAILTO` | – | Polite-pool contact for OpenAlex and Crossref |
| `OPENALEX_API_KEY` | – | Optional secret, sent as a Bearer header |
| `UDR_TAVILY_MONTHLY_LIMIT` | `1000` | `0` disables Tavily |
| `UDR_INTERNAL_DOMAINS` | – | Comma-separated suffixes, e.g. `brenk.local,corp.example` |
| `UDR_FETCH_TIMEOUT_S` | `30` | |
| `UDR_MAX_HTML_MB` / `UDR_MAX_PDF_MB` | `10` / `25` | |

## Testing

- **Real clients on a mock network.** The gateway tests run the real clients against one recording
  `httpx.MockTransport`. For every denylist variant and entry point they assert that no request
  went out at all.
- **ddgs bypasses the socket block.** ddgs uses `primp` (Rust), which bypasses the Python socket
  block in `tests/conftest.py`. Tests therefore always inject its search function; only live tests
  reach the real one.
- **Mutation checks.** Six gateway mutants (each a confidentiality or budget hole) are each caught.
  So are the guard's resolve-all rule and the ledger's file lock.
- **Live.** `tests/live/test_live_outbound.py` uses neutral queries and spends one Tavily credit if
  a key is set.
