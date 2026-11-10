# PayPal sandbox integration

**Status (2026-11-10):** verified against the **real PayPal sandbox** (`https://api-m.sandbox.paypal.com`) and
offline against a local HTTP fake (`procure/paypal_fake_http.py`). Items still marked *(to verify)* below have not
been confirmed against PayPal itself.

### Real sandbox results (2026-11-10, PT)
Credentials came from a file outside the repo (`PAYPAL_SECRETS_FILE=...`) and are never printed. Order IDs are sandbox-only.

| Run (PT) | Command | Result |
|---|---|---|
| 09:21 | `sandbox_smoke.py` | All OK: OAuth; create order `0WX92963WT911654D` (`PAYER_ACTION_REQUIRED`); idempotent re-create returns the same order; read-back 1.00 USD; capture before approval → expected `422 ORDER_NOT_APPROVED`; `GET /v1/customer/disputes` works (0 items) |
| 09:21 | `sandbox_smoke.py --capture 0WX92963WT911654D` | `[FAIL]` as expected: nobody approved that order (`ORDER_NOT_APPROVED`). A sandbox Personal (buyer) login is needed to approve it at the printed `sandbox.paypal.com/checkoutnow` link |
| 09:24 | `sandbox_smoke.py --test-card` | All OK: order `7FY86708M68742001`; PayPal's sandbox test card confirms the payment source (`APPROVED`); capture `9FM537731D290830Y` `COMPLETED`; second capture returns the same capture (`ORDER_ALREADY_CAPTURED` path); disputes for that capture: none |
| 09:35 | demo video v1 (archived as `materials/demo_video_v1.mp4`), desk on `--sandbox` | sandbox order `2FA402224X238943Y` → test-card approval → capture `6MR08543363319036` `COMPLETED`. Re-checked 11-10 with `GET /v2/checkout/orders/2FA402224X238943Y` (COMPLETED, 2,200.00 USD, capture `6MR08543363319036`). A reviewer read `2FA402224X230943Y` / `6MR085433G331903G` off the small monospace text; that order ID returns 404 in the sandbox, so it was a misreading. v2 zooms in on the IDs |
| 10:22 | **demo video v2 (final)**, desk on `--sandbox` | sandbox order `1S38017531849904R` → test-card approval → capture `8T206088RF006424G` `COMPLETED`, USD 2,200.00. These are the IDs zoomed in `materials/demo_video.mp4` (and `docs/screenshots/13_sandbox_desk_paid.png`); the builder writes what the page showed to `materials/demo_video_ids.json`, and `GET /v2/checkout/orders/1S38017531849904R` confirms them |
| 10:22 | demo video v2, terminal segment: `sandbox_smoke.py --test-card` | order `6CS51398L8633780W` → capture `52J66303ES0530434` `COMPLETED` (1.00 USD) |
| 09:23 | approval desk, `web/server.py --sandbox` | Over-budget laptop request → manager + finance approve → signed AP2 mandate → sandbox order `5N4430542X629603H` → "Approve with sandbox test card" → capture `05232883K2379663K` `COMPLETED`, USD 2,200.00; read-back verified against the mandate; audit chain valid (14 entries) |

**Test card.** With no sandbox buyer login, the desk's sandbox mode and `--test-card` approve the order by
confirming PayPal's published sandbox test Visa as the payment source (`POST /v2/checkout/orders/{id}/confirm-payment-source`).
This stands in for the buyer's click on the checkout page. Capture then runs through exactly the same guarded path
(return_url handler, read-back, mandate check). The client refuses this call unless the base URL is the sandbox.

## What runs where
| Mode | PayPal side | Used by |
|---|---|---|
| Offline emulation (default) | in-memory Orders v2 / Disputes v1 built from PayPal's OpenAPI specs (`procure/paypal_mock.py`, IDs start with `MOCK`) | dashboard, scenarios, metrics |
| Local HTTP fake | a real HTTP server on 127.0.0.1 in front of that emulation, with OAuth2, Bearer tokens, `PayPal-Request-Id` replay and scripted faults | `tests/test_paypal_rest_http.py`, `scripts/sandbox_smoke.py --fake` |
| Real sandbox | `https://api-m.sandbox.paypal.com` through `PayPalREST` (`procure/paypal.py`) | `scripts/sandbox_smoke.py`, `--sandbox` flags |

The same client class (`PayPalREST`) talks to the fake and to the real sandbox; only the base URL and credentials change.

## Setup
1. Log in at developer.paypal.com → **Apps & Credentials** → **Sandbox** → create an app → copy Client ID and Secret.
2. `cp .secrets.example .secrets` and fill in `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET`. `.secrets` is git-ignored.
   Live API hosts are refused: this project is sandbox-only.
3. Sandbox roles: the sandbox **Business** account owns the REST app and plays the **supplier (payee)**; the sandbox
   **Personal** account plays the **company buyer** who approves each order.

## One command
```bash
python3 scripts/sandbox_smoke.py                 # OAuth -> create $1.00 order -> read back -> capture (expects 422) -> list disputes
# open the printed approve link with the sandbox Personal account, approve, then:
python3 scripts/sandbox_smoke.py --capture <ORDER_ID>
python3 scripts/sandbox_smoke.py --capture-id <CAPTURE_ID>       # disputes filed against that capture
python3 scripts/sandbox_smoke.py --dispute <DISPUTE_ID>
python3 scripts/sandbox_smoke.py --test-card     # sandbox only: approve with PayPal's sandbox test card, capture twice
python3 scripts/sandbox_smoke.py --fake          # the whole flow offline, no credentials
```
Each step prints `[ok]`, `[expected]` or `[FAIL]`; a JSON summary goes to `out/` (git-ignored). Secrets and tokens are
never printed. The dashboard and scenarios use the sandbox with `python3 web/server.py --sandbox` and
`python3 scenarios/run_all.py --sandbox`.

## Error paths
| PayPal answer | What the client does |
|---|---|
| 422 `ORDER_NOT_APPROVED` / `PAYER_ACTION_REQUIRED` on capture | raises `BuyerApprovalRequired`; the agent waits at `awaiting_buyer_approval` with the approve link; capture runs again from the `return_url` handler |
| 422 `ORDER_ALREADY_CAPTURED` | reads the order back and returns the existing capture (`already_captured: true`); no second charge |
| 401 (expired or invalid token) | fetches a new OAuth token once and repeats the call |
| 429 | retries with the same `PayPal-Request-Id`; waits for `Retry-After` (capped at 30 s), otherwise exponential backoff |
| 5xx, timeout, dropped connection | retries with the same `PayPal-Request-Id` (max 3 retries); a POST **without** that header is never retried |
| other 4xx | raised at once as `PayPalHTTPError` (status, PayPal error body, first `issue`) |

## Idempotency
Following PayPal's idempotency guide, the `PayPal-Request-Id` is unique per request and per call type:
- **create order:** `uuid5("create:" + request reference)`, so the same purchase request never creates two orders,
  even across a restart or after a timeout where PayPal did create the order;
- **capture:** one id per capture call, reused by that call's retries. Not one id per order: a capture refused with
  `ORDER_NOT_APPROVED` has to be retried after the buyer approves, and PayPal replays the stored result of a known
  id. Double capture is still impossible because PayPal allows one capture per `intent=CAPTURE` order;
- **evidence:** `uuid5("evidence:" + dispute id + note hash)`.
After the order is created, the agent reads it back (`GET /v2/checkout/orders/{id}`) and checks the amount against the
signed mandate before anyone pays.

## Disputes: buyer-side design
PayPal's Disputes v1 API lists and shows disputes and offers actions on them, but has **no endpoint for a buyer to create a
dispute**. So:
1. On a short or wrong delivery, the agent reconciles the receipt against the order and **drafts the claim**: reason
   `MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED`, the exact refund amount and a factual message (audit event
   `dispute_prepared_for_buyer`, status `dispute_pending_buyer_filing`).
2. A person **files it in the sandbox Resolution Center** with the buyer account (copying the draft).
3. The agent **tracks it**: `ProcurementAgent.track_dispute()` finds it with
   `GET /v1/customer/disputes?disputed_transaction_id=<capture id>` and reads it with `GET /v1/customer/disputes/{id}`
   (audit event `dispute_tracked`).
4. When PayPal asks the buyer for evidence (status `WAITING_FOR_BUYER_RESPONSE`), the agent can submit it with
   `POST /v1/customer/disputes/{id}/provide-evidence` (multipart: JSON part `input` + optional `evidence-file` parts).
   In the sandbox, `POST .../require-evidence` (sandbox-only) moves an `UNDER_REVIEW` dispute to that state.

*(to verify with real sandbox credentials)*: which account's REST credentials can list a dispute filed by the Personal
account (in a single developer account we expect the Business account's app, i.e. the payee side, to see it); the exact
`evidence_type` PayPal accepts from a buyer; whether PayPal replays failed (4xx) results for a known `PayPal-Request-Id`.
