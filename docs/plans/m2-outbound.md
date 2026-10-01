# M2 implementation plan — Outbound gateway and retrieval adapters

Source of truth: [PRD.md](../../PRD.md) §4 M2, §3.2 (confidentiality), §3.3 (search, fetch, budget).
Rules: [AGENTS.md](../../AGENTS.md). Status: [IMPLEMENTATION.md](../../IMPLEMENTATION.md). If this
plan and the PRD disagree, the PRD wins. Raise the conflict instead of choosing.

## 1. Scope

**In scope:**
- the single egress package `src/app/adapters/outbound/`, containing:
  - the denylist (store and matcher);
  - the private-URL guard;
  - the `reason`-role query sanitizer;
  - the outbound log;
  - Tavily search and extract;
  - `ddgs` search;
  - OpenAlex, Crossref and arXiv clients;
  - the HTTP fetcher with HTML and PDF text extraction;
  - credit ledgers (run and month) with provider switching;
- the AST egress guard test;
- `udr denylist add|remove|list`.

**Out of scope, and the milestone that owns it:**
- junk gates beyond "too little text", dedup, notes and claims: M3;
- `source_strategies.toml` and tier scoring: M3;
- the search planner and lenses: M5;
- plan approval UI and API: M5–M7.

M2 delivers the calls; it decides nothing about *what* to search.

## 2. Decisions for M2

| Topic | Decision | Reason |
|---|---|---|
| Tavily client | Direct REST via `httpx` (`POST /search`, `/extract`, Bearer key); no `tavily-python` | Status codes 401/429/432/433 are visible directly, and `httpx.MockTransport` fakes it like `OllamaAdmin` in M1. |
| Prepare vs send | `prepare_query()` runs denylist → sanitizer → denylist and returns a `PreparedQuery`. Every send path re-checks the denylist on the exact payload (query text and every URL), deterministically. | Approved plan queries must be sent as approved (no second LLM rewrite), yet nothing reaches a provider unchecked (AC1). |
| Denylist matching | Fold each text two ways: umlaut → `ae/oe/ue`, and diacritics stripped. Both use NFKC and casefold. Then tokenize into alphanumeric words. A term matches when its joined tokens equal the joined tokens of any contiguous run of text tokens. | Covers case, umlaut, diacritic, hyphen and spacing variants (`Müller-Werke` = `mueller werke` = `MullerWerke`), but never matches inside a word, so short terms like `AG` do not hit `Tagung`. |
| Private-URL guard | Scheme http(s). An IP literal must be `is_global`. A host must contain a dot and must not end in `UDR_INTERNAL_DOMAINS`. **Every resolved address** of the host must be global (resolver injected). Re-checked on each redirect hop (max 5, followed manually). | PRD §3.2. DNS resolution also catches a public-looking name that points into the LAN. |
| Retries | The gateway owns retry loops; providers make exactly one attempt and raise typed errors. Tavily: 2 retries (1 s, 2 s) on timeout, 5xx or 429. ddgs: 3 tries, then `SearchUnavailable`. Scholarly APIs: 2 retries. | One place logs every attempt (AC7) and counts credits. |
| Provider switching | Per run, state is `tavily` or `ddgs`. Switch before a search when the run ledger cannot afford it, the month ledger is at its limit, or no key is configured. Switch after HTTP 432/433 or 401/403. Each switch emits `provider_switched {from, to, reason}` exactly once. Extract fallback is skipped once switched. | PRD §3.3: the run never fails because of Tavily. |
| Month ledger | `data/tavily-ledger.json` `{month: "YYYY-MM", credits}`, reset on a month change, `fcntl.flock` around read-modify-write. Warning event at 80 %. | Shared by concurrent processes (CLI and service). |
| Fetch | Local first, `follow_redirects=False`, streamed with caps (HTML 10 MiB, PDF 25 MiB). PDF detected by content type, `%PDF` magic or `.pdf` path. Charset: header, then `<meta>`, then `charset_normalizer`. Fallback to Tavily Extract only for network/HTTP failures or HTML text under 300 chars. Never for too-large or unsupported types. | PRD §3.3. The full junk gates are M3. |
| Throttle | Minimum interval per host, default 1 s. Overrides: `export.arxiv.org` 3 s (arXiv API terms), `api.openalex.org` / `api.crossref.org` 0.1 s, `api.tavily.com` 0. | PRD §3.3, plus the arXiv rule. |
| User-Agent | `local-ultradeepsearch/<version> (research tool)` for websites. `mailto` only goes to OpenAlex and Crossref. | No URL in the UA (SEC lesson). The owner's email is not sent to arbitrary sites. |
| OpenAlex key | Optional `OPENALEX_API_KEY` sent as a Bearer header, never in the URL | OpenAlex now offers free keys with 10× the budget; the header keeps it out of logs. |
| XML | arXiv Atom parsed with `defusedxml` | Untrusted XML |
| ddgs offline safety | `ddgs` uses `primp` (Rust), which bypasses the Python socket block in `conftest`. Tests therefore always inject a fake search function, and the default factory is reached only by `live` tests. | Keeps the suite offline. |

**Dependencies** (licences audited): `trafilatura` (Apache-2.0), `pypdfium2` (BSD-3/Apache-2.0),
`ddgs` and `primp` (MIT), `defusedxml` (PSF), `charset-normalizer` (MIT). Two transitive packages
are outside the AGENTS.md §5.5 list and go to the owner: `tld` (MPL-1.1/GPL/LGPL, via courlan) and
`certifi` (MPL-2.0, via httpx since M1).

## 3. Layout

```
src/app/adapters/outbound/
  __init__.py
  errors.py      OutboundBlocked (DenylistBlocked, PrivateUrlBlocked), ProviderError
                 (TransientProviderError, PlanLimitError, ProviderAuthError), SearchUnavailable
  denylist.py    fold_variants(), tokens(), Denylist (load/save/add/remove/find)
  guard.py       check_url(url, internal_domains, resolve) -> reason | None
  log.py         OutboundRecord, OutboundLog (jsonl, thread-safe)
  ledger.py      search_credits(), extract_credits(), RunLedger, MonthLedger
  throttle.py    HostThrottle (per-host minimum interval; injectable clock/sleep)
  sanitizer.py   Sanitizer (LLMService reason role, cached) -> SanitizedQuery
  extract.py     decode_html(), html_to_text() (trafilatura), pdf_to_pages() (pypdfium2)
  providers.py   TavilyApi, DdgsSearch, OpenAlexApi, CrossrefApi, ArxivApi, HttpGetter
  gateway.py     OutboundGateway (per run): prepare_query, search_web, search_scholarly, fetch
src/app/prompts/outbound.py   SANITIZER_SYSTEM, SANITIZER_USER
src/app/cli.py                + `udr denylist add|remove|list`
tests/test_egress_guard.py    AST rule, with a violating-input case
```

**Key types:**
- `PreparedQuery(original, sent, removed_terms)`
- `SearchHit(title, url, snippet, provider)`
- `ScholarlyRecord(source, title, url, doi, year, authors, venue, cited_by_count, is_retracted, oa_url, abstract, kind)`
- `Document(url, final_url, content_type, title, text, pages, html, via)`
- `FetchFailure(url, reason)`

## 4. Settings added

| Variable | Default | Notes |
|---|---|---|
| `TAVILY_API_KEY` | none | Secret. Without it, search uses ddgs from the start. |
| `OPENALEX_MAILTO` | none | Polite-pool contact for OpenAlex and Crossref |
| `OPENALEX_API_KEY` | none | Optional secret |
| `UDR_TAVILY_MONTHLY_LIMIT` | 1000 | Credits per calendar month |
| `UDR_INTERNAL_DOMAINS` | empty | Comma-separated suffixes, e.g. `brenk.local,corp.example` |
| `UDR_FETCH_TIMEOUT_S` | 30 | |
| `UDR_MAX_HTML_MB` / `UDR_MAX_PDF_MB` | 10 / 25 | |

`conftest` also scrubs `TAVILY_API_KEY`, `OPENALEX_MAILTO` and `OPENALEX_API_KEY`.

## 5. Gateway behaviour

**`prepare_query(query, step)`:**
1. Denylist check on the original; a hit raises `DenylistBlocked`.
2. Sanitizer (cached per text).
3. If the result is empty, raise `DenylistBlocked("empty after sanitizing")`.
4. Denylist check on the result; a hit raises.

Every blocked step logs one `blocked` line.

**`search_web(prepared, step, include_domains=(), max_results=10)`:**
1. Denylist check on `prepared.sent`.
2. Choose the provider (switch rules in §2).
3. Tavily: log each attempt, then charge 1 credit to both ledgers on success.
4. ddgs: hits only.

`include_domains` passes through to Tavily; ddgs ignores it. A `provider_switched` event is emitted
once per switch.

**`search_scholarly(prepared, step, source)`:** denylist check, then one source with retries. No
credits are charged.

**`fetch(url, step)`:**
1. Denylist check on the URL (percent-decoded).
2. Guard check.
3. Local GET loop over redirects, re-checking the denylist and guard on each hop.
4. Classify and extract.
5. On a qualifying failure, fall back to Tavily Extract (only if the ledgers allow, the provider is
   not switched, and the URL passed the guard). Charge `ceil(successful/5)` credits.

Returns a `Document` or a `FetchFailure`. Reason codes: `blocked_denylist`, `blocked_private`,
`too_large`, `unsupported_type`, `http_<status>`, `timeout`, `network`, `too_many_redirects`,
`empty_text`, `extract_failed`.

**Log line** (AC7): one per network attempt and one per blocked attempt. Fields: `ts, step,
provider, original_query, sent_query, removed_terms, url, status, credits`, null when not
applicable. `status` is the HTTP code, `ok`, `blocked:<reason>` or `error:<kind>`.

## 6. Work steps (red → green → gate → commit, as in M1)

1. **Settings and conftest scrub.** Commit: `feat(config): outbound settings`.
2. **Denylist and `udr denylist`.** AC1 table, word-boundary cases, file round trip.
   Commit: `feat(outbound): denylist with variant-folding matcher`.
3. **Guard.** AC3 table, including a resolver returning private addresses, IPv6 and redirects
   (redirects are covered in step 8). Commit: `feat(outbound): private-URL guard`.
4. **Log, ledgers, throttle.** AC4 (10 searches plus 12 URLs = 13 credits), month rollover, 80 %
   warning, lock, throttle intervals. Commit: `feat(outbound): log, credit ledgers and host throttle`.
5. **Sanitizer.** Scripted LLM: removal, caching, empty result, prompt carries the context.
   Commit: `feat(outbound): reason-role query sanitizer`.
6. **Extraction.** HTML with and without a declared charset, meta charset, a PDF built in-test
   with pypdfium2, empty text. Commit: `feat(outbound): HTML and PDF text extraction`.
7. **Providers.** `MockTransport` fixtures for Tavily (200/401/429/432/433/5xx), OpenAlex,
   Crossref and arXiv (defusedxml), the streamed GET with caps, and ddgs via an injected function.
   Commit: `feat(outbound): provider clients`.
8. **Gateway.**
   - AC1 end-to-end: no fake ever receives a term, across search, scholarly, fetch and extract.
   - AC2 and AC5 (each switch trigger, emitted once).
   - AC7: exact line counts, including retries and redirects.
   - Fetch fallback rules and all edge cases.

   Commit: `feat(outbound): run-scoped gateway with provider switching`.
9. **Egress AST test** (AC6). Network modules (`httpx`, `requests`, `urllib.request`, `urllib3`,
   `http.client`, `socket`, `aiohttp`, `tavily`, `ddgs`, `primp`, `ollama`) may only be imported in
   `src/app/adapters/outbound/**` and `src/app/adapters/ollama_transport.py`. It needs one
   violating-input test. Commit: `test: egress guard`.
10. **Composition, live tests, docs.**
    - `bootstrap.build_gateway(rt, run_dir, credit_cap, confidential_context)`.
    - `tests/live/test_live_outbound.py`: ddgs, OpenAlex, Crossref, arXiv, one fetch and one PDF;
      Tavily only if a key is set (1 search costs 1 credit).
    - `/documentation-update`.

## 7. Acceptance criteria → tests

| PRD M2 AC | Proven by |
|---|---|
| 1 denylist variants never leave; `DenylistBlocked`; `blocked` logged | `test_denylist.py` (matcher table), `test_gateway.py::test_no_provider_ever_sees_a_denylisted_term` (parametrized over variants × entry points) |
| 2 sanitized query still matching → blocked | `test_gateway.py::test_sanitizer_output_is_rechecked` |
| 3 guard table incl. redirects | `test_guard.py`, `test_gateway.py::test_redirect_into_private_space_is_never_requested` |
| 4 10 searches + 12 URLs = 13 credits | `test_ledger.py::test_prd_credit_example`, `test_gateway.py::test_credits_are_charged_per_prd` |
| 5 run cap / 432 / month limit → ddgs + event | `test_gateway.py::test_switch_*` |
| 6 AST egress rule + violating input | `test_egress_guard.py` |
| 7 one complete line per request | `test_gateway.py::test_log_lines_*` |

| Edge case | Test |
|---|---|
| ddgs blocked → 3 tries → `SearchUnavailable` | `test_gateway.py::test_ddgs_gives_up_after_three_tries` |
| Tavily timeout → 2 retries | `test_gateway.py::test_tavily_timeout_retries_twice` |
| Oversized HTML/PDF → reason code | `test_providers.py::test_stream_cap_*`, `test_gateway.py::test_too_large_is_not_retried_via_extract` |
| Undeclared charset | `test_extract.py::test_detects_undeclared_charset` |
| All providers down | `test_gateway.py::test_everything_down_returns_typed_failures` |

## 8. Live verification (manual; results go to IMPLEMENTATION.md)

```
uv run pytest -m live tests/live/test_live_outbound.py
```

This runs the real APIs with neutral queries. Tavily runs only with `TAVILY_API_KEY` in `.env` and
spends one search credit.
