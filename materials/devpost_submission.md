# Devpost submission draft: GuardedBuyer

> Copy each block into the matching field of "Enter a Submission" on paypalaihackathon.devpost.com.
> Every number below comes from files in this repo (`docs/comparison_200.md`, `docs/sandbox.md`). Keep them in sync.
> Placeholders that only the team can fill: `<REPO_URL>`, `<YOUTUBE_URL>`.

---

## Project name
GuardedBuyer

## Elevator pitch / tagline (max 200 chars)
An AI purchasing agent for small businesses that pays with PayPal but can't overspend: AP2-signed mandates, human approvals, and automatic dispute drafts when deliveries fall short.

## Inspiration
Small businesses buy supplies constantly, usually through one office manager with a company card and a spreadsheet. Two things keep going wrong: someone buys more than they're allowed to, and when a delivery doesn't match the order, nobody claims the money back.

AI agents can now shop and pay, which makes the first problem worse. An agent with a payment tool will follow an instruction hidden in a supplier's product listing. Most agentic-commerce tooling, including PayPal's own merchant products, helps **merchants** get paid by agents. We built for the **buyer's** side: let an agent do the purchasing, make it provably unable to spend money it wasn't authorized to spend, and have it chase refunds on its own.

## What it does
An employee types a request in plain English, for example "Buy 2 laptops for the new engineers". GuardedBuyer then:
1. **Understands and compares.** An LLM maps the request to a catalog item, compares supplier quotes and explains its choice. Supplier text is treated as untrusted input.
2. **Applies company policy.** Department budgets, allowed categories, blocked merchants, a hard cap, and an approval chain (manager above $500; finance above $2,000 or when over budget). Only humans can approve: the agent has no approve tool.
3. **Parks blocked purchases on an approval desk.** The approver sees why the purchase was blocked, the budget overrun, the agent's refused payment attempt, the chosen quote and its reason, and the exact limits that will be signed. Approvers can add a note or reject.
4. **Signs an AP2 Intent Mandate on release.** The mandate pins merchant, SKU, quantity, maximum amount and expiry, and is signed with Ed25519. Every payment is checked against it.
5. **Pays with PayPal.** It creates a PayPal Orders v2 order and waits for buyer approval. Capture only happens after the order is read back from PayPal and matches the mandate. Retries are idempotent (`PayPal-Request-Id`), so there are no double orders or double captures.
6. **Reconciles the delivery.** If the shipment is short, the wrong SKU, or overcharged, it drafts a PayPal dispute for exactly the shortfall with a factual message, then tracks it and adds evidence through the Disputes API.
7. **Proves it.** Every step, including every blocked attempt, goes into a SHA-256 hash-chained audit log that anyone can verify.
8. **Shows its evidence.** An evaluation explorer built with **AG Grid** lists all 200 synthetic requests and how each guardrail configuration handled them.

## How we built it
- **PayPal (core):**
  - Orders v2: create with `payment_source.paypal.experience_context`, read-back, and capture, with `ORDER_NOT_APPROVED` and `ORDER_ALREADY_CAPTURED` handled.
  - Disputes v1: list, get, and provide-evidence (multipart).
  - OAuth2 client credentials with token refresh.
  - Retries with exponential backoff and Retry-After that only re-send a POST when it carries a `PayPal-Request-Id`.
  - Verified on the **real PayPal sandbox**, including an end-to-end over-budget purchase captured from the approval desk.
  - Also integrated the official **PayPal Agent Toolkit** (Python) as an alternative backend. Its payment tools are only ever called by our guarded executor and never handed to the LLM.
- **AI:**
  - An LLM tool-calling agent with five narrow tools, backed by any OpenAI-compatible endpoint; we used `deepseek-v4.1-flash`.
  - LLM helpers: request parsing (text to SKU and quantity), quote recommendation with a reason, and the factual dispute message.
  - Every model output is validated, and each LLM step has a deterministic fallback, so the demo also runs offline.
- **AP2:** the mandate's intent uses the field names of AP2's `IntentMandate`. Spend limits live in a clearly separated extension, signed and verified by us.
- **Guardrails the model can't bypass:**
  - The price comes from the merchant quote, never from the model.
  - A scope guard keeps purchases within what the employee asked for.
  - Merchants whose listings carry injections are quarantined.
  - The mandate is checked before the PayPal call and again against PayPal's own read-back.
- **UI:**
  - Stdlib Python server, vanilla JS, Lucide icons.
  - A dashboard, the one-page approval desk, and the AG Grid Community evaluation explorer.
  - Render Blueprint (`render.yaml`) for one-click hosting.
- **Testing:**
  - 130+ automated tests.
  - A local HTTP fake of PayPal REST with fault injection: dropped connections, 5xx, 429, expired tokens, idempotency replay.
  - `scripts/sandbox_smoke.py` for the real sandbox.
  - A 200-request evaluation with three configurations.

## Challenges we ran into
- **Buyers can't open disputes through the API.** The Disputes API reads and acts on disputes, but there is no buyer-side create endpoint. So the agent drafts the claim (reason, exact amount, message), a person files it in the Resolution Center, and the agent tracks it and adds evidence via the API.
- **Buyer approval is a human step by design.** You can't capture before the payer approves (`ORDER_NOT_APPROVED`). We made that explicit: the return_url handler resumes capture. In the sandbox, PayPal's test card can stand in for the buyer's click, and the client refuses to do this outside the sandbox.
- **Idempotency is per call type.** A deterministic capture request ID replayed a cached 422 after the buyer had approved. Each capture attempt now gets its own ID, and a second capture is resolved through `ORDER_ALREADY_CAPTURED`.
- **Prompt injection isn't a detection problem.** Our detector misses polite injections on purpose in the test set. The boundary has to live in deterministic code: scope, policy, approvals and mandate.

## Accomplishments that we're proud of
- **The full flow runs on the real PayPal sandbox:**
  1. Over-budget request blocked.
  2. Manager and finance approve.
  3. Signed AP2 mandate.
  4. PayPal sandbox order.
  5. Approval.
  6. Capture `COMPLETED`.
  7. Read-back verified.
  8. Audit chain valid.
  - The demo video records this run live: sandbox order `1S38017531849904R`, capture `8T206088RF006424G` (USD 2,200.00). The terminal segment is a separate smoke run: order `6CS51398L8633780W`, capture `52J66303ES0530434`.
- **On 200 synthetic purchase requests** (seed 20261110, scripted worst-case agent):
  - Full guardrails stopped **91/96 attack cases (94.8%)** and **97/102 unauthorized attempts (95.1%)**.
  - A classic spend policy stopped 60/96 (62.5%) attack cases and 66/102 attempts; no guardrails stopped 0.
  - **No legitimate request was blocked (0/74)** in any configuration.
  - All **30/30 delivery mismatches** were handled.
- **We publish our misses.** All 5 are the same pattern, listed in `docs/failure_cases.md` and one click away in the explorer.
- **A red-team scenario.** A fully compromised agent obeys a fake "SYSTEM NOTICE": it tries to approve itself, buy gift cards, switch stores and order 400 units. Every attempt is blocked and logged, and the employee's real order still goes through.

## What we learned
- "LLM proposes, code disposes." Models are great at understanding a request and explaining a choice; they shouldn't decide whether money moves.
- A mandate is most valuable when it's checked twice: before the payment call, and against what the payment provider actually recorded.
- Post-purchase work (reconciliation, disputes) is where small businesses quietly lose money, and it suits an agent because the facts are structured.

## What's next
- Mandates issued by a real AP2 credential provider instead of a local key.
- Webhooks (`CHECKOUT.ORDER.APPROVED`, `CUSTOMER.DISPUTE.*`) instead of polling.
- Orders v2 package tracking to time the delivery check.
- Multi-line orders and accounting export from the audit log.
- A pilot with a few small offices.

## Built with
`python` · `paypal-orders-v2` · `paypal-disputes-v1` · `paypal-rest-api` · `paypal-sandbox` · `paypal-agent-toolkit` · `ap2` · `llm` · `deepseek` · `openai-compatible-api` · `ed25519` · `ag-grid` · `render` · `javascript` · `html` · `css` · `lucide`

## Sponsor tools to tick on the form
- **AG Grid**: `/evaluation` (AG Grid Community, MIT, vendored at `web/static/vendor/ag-grid/`).
- **Render**: `render.yaml` Blueprint (prepared, not yet deployed).

## Tools: which AI and how (required by the overview page)
- **LLM:** `deepseek-v4.1-flash` through an OpenAI-compatible API. It does request parsing, quote recommendation with a reason, the dispute message, and the tool-calling agent loop.
- **Validation and fallback:** all LLM output is validated, and deterministic fallbacks keep the demo runnable without a key.
- **Separation of duties:** the LLM can't approve, set prices, mint mandates, or call PayPal directly.
- **PayPal Agent Toolkit (Python):** an alternative PayPal backend (`--toolkit`).

## Significant updates during the submission period (Oct 1 – Nov 12, 2026)
The project existed before the hackathon as an offline prototype. During the submission period we added:
1. **Real PayPal sandbox integration:**
   - Credentials come from a git-ignored file.
   - Idempotent retries, token refresh, and `ORDER_NOT_APPROVED` / `ORDER_ALREADY_CAPTURED` handling.
   - Disputes evidence upload.
   - A sandbox smoke test, run successfully on Nov 10, 2026.
2. **A one-page approval desk:** a blocked over-budget purchase goes through human approval, becomes a newly signed AP2 mandate, then a PayPal order and capture.
3. **A 200-request evaluation:** with/without guardrails, with sample and denominators spelled out, plus an AG Grid evaluation explorer.
4. **A local HTTP fake of PayPal REST** with fault injection, and 30+ new tests.
5. **A Render Blueprint, a refreshed README, and the demo video.**

## Testing instructions for judges
No account is needed for the default mode.
```bash
git clone <REPO_URL> && cd guardedbuyer
python3 -m pip install -r requirements.txt      # one dependency: cryptography
python3 web/server.py --port 8000               # offline PayPal emulation, no keys
# open http://127.0.0.1:8000/approvals?demo=1   (approval desk)
#      http://127.0.0.1:8000/evaluation         (AG Grid evaluation explorer)
#      http://127.0.0.1:8000/                    (dashboard + replay scenarios)
python3 -m pytest -q                            # full test suite
```
**Real PayPal sandbox** (your own sandbox app):
1. `cp .secrets.example .secrets` and fill in the sandbox client ID and secret.
2. Run `python3 scripts/sandbox_smoke.py --test-card`.
3. Run `python3 web/server.py --sandbox`, then in the desk use "Approve with sandbox test card" or approve with a sandbox Personal account.

**Live LLM:** set `OPENAI_BASE_URL`, `OPENAI_API_KEY` and `LLM_MODEL` in `.env` and add `--llm`.

All demo data is synthetic. No live PayPal hosts are ever contacted.

## Links
- Repository: `<REPO_URL>` (MIT licence, © 2026 elidot)
- Video: `<YOUTUBE_URL>`
