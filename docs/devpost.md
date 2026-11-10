# GuardedBuyer: an AP2-guarded procurement agent for small businesses (Devpost draft)

> Draft for the Devpost "About the project" fields. Numbers come from `docs/metrics.md` (synthetic test set). Keep every
> claim in sync with the repo before submitting.

## Inspiration
Small businesses buy supplies all the time: paper, toner, laptops, peripherals. Usually one office manager does it,
with a company card and a spreadsheet. Two things go wrong again and again: someone buys more than they are allowed to,
and the delivery doesn't match the order and nobody claims the money back.

AI agents can now shop and pay, which makes the first problem worse: an agent with a payment tool will happily follow an
instruction hidden in a supplier's product listing. Most agentic-commerce work, including PayPal's own Agent Ready and
Store Sync, helps **merchants** get paid by agents. We wanted to build the **buyer's** side: let an agent do the
purchasing, but make it provably unable to spend money it was not authorized to spend, and make it chase refunds by itself.

## Significant updates during the submission period (Nov 2026)
- Real PayPal sandbox wiring: credentials from a git-ignored `.secrets`, idempotent retries with `PayPal-Request-Id`,
  token refresh, `ORDER_ALREADY_CAPTURED` handling, Disputes v1 evidence; tested over HTTP against a local PayPal fake;
  `scripts/sandbox_smoke.py` switches to the real sandbox in one command.
- A one-page approval desk: blocked over-budget purchase → reasons, budget, mandate constraints and audit chain → human
  approval → newly signed AP2 mandate → PayPal order → capture.
- A 200-request comparison of no guardrails / policy only / full guardrails with the sample and denominators spelled out.

## What it does
An employee types a request in plain English ("Buy 2 laptops for the new engineers"). GuardedBuyer:
1. **Understands and compares.** An LLM maps the request to a catalog item, compares supplier quotes and explains its choice.
   Supplier text is fenced as untrusted, and listings with injection indicators are excluded.
2. **Applies company policy.** Department budgets, category allow-list, blocked merchants, hard cap, and an approval
   chain (manager above $500, finance above $2,000 or over budget). Only humans can approve; the agent has no approve tool.
3. **Issues an AP2 Intent Mandate.** A signed mandate (AP2 `IntentMandate` fields plus our spend-limit extension) pins merchant,
   SKU, quantity, amount and expiry. Every payment is verified against it, so a tampered charge is refused.
4. **Pays with PayPal.** It creates an Orders v2 order, sends the buyer to PayPal's approval page, captures on the return_url
   callback, then reads the order back from PayPal and checks it against the mandate again.
5. **Reconciles the delivery.** If the shipment is short or the SKU is wrong, it prepares a PayPal dispute
   (`MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED`) for exactly the shortfall, with a factual message, and tracks it.
6. **Proves it.** Every step, including every blocked attempt, goes into a SHA-256 hash-chained audit log that anyone can verify.

The web dashboard shows the whole flow live: request, quote comparison with reasons, approval buttons, mandate details,
PayPal order state, the return_url page, delivery check, dispute tracking, and the audit log with blocked events in red.
One click replays four scenarios, including a red-team run in which a supplier listing tries to hijack the agent.

## How we built it
- **Python, stdlib first.** Policy engine, mandate signing (Ed25519 via `cryptography`), hash-chained audit log, and a
  dependency-free web server and dashboard (vanilla JS).
- **LLM layer.** Any OpenAI-compatible endpoint (we used deepseek-v4.1-flash). A tool-calling loop with five narrow tools.
  Every model output is validated (the SKU must exist, the quote index must be in range, the dispute note must cite the SKU),
  and each LLM step has a deterministic fallback, so the demo also runs fully offline.
- **Deterministic guardrails the model can't bypass.** The model never sets a price, never chooses the charge amount,
  never approves, and never mints a mandate. On top of that: a scope guard (purchases must stay within what the employee
  asked for), a quarantine for merchants whose listings carry injections, and the mandate check before and after the
  PayPal call.
- **PayPal.** Orders v2 (create with `payment_source.paypal.experience_context`, payer-action approval, capture) and
  Disputes v1 (get/list, accept-claim). We integrated the official **PayPal Agent Toolkit** (Python) as a backend, but its
  payment tools are only ever called by our guarded executor and are never handed to the LLM. For offline judging we built
  an emulation of Orders v2 / Disputes v1 from PayPal's published OpenAPI specs (field names, statuses, error bodies,
  idempotency), and the toolkit's own handlers run against it unchanged.
- **AP2.** The mandate's `intent` uses the field names of AP2's `IntentMandate`. AP2 has no amount field, so spend limits
  live in a clearly separated extension that we sign and verify ourselves.
- **Evaluation.** A synthetic case set (normal, over-authority, prompt injection, delivery mismatch) run against three
  configurations (no guardrails / policy only / full) with a scripted worst-case agent that always attempts the attack.

## Challenges we ran into
- **Buyers can't open disputes through the API.** PayPal's Disputes API lets you list, read and act on disputes, but
  there is no endpoint for a buyer to create one. We redesigned that step: the agent prepares the claim (reason, amount,
  message), a person files it in the Resolution Center, and the agent tracks it by ID.
- **Buyer approval is a human step by design.** An order can't be captured until the payer approves it on PayPal's page
  (`ORDER_NOT_APPROVED` otherwise). We made that explicit in the flow and built the return_url handler that resumes capture.
- **Prompt injection is not a detection problem.** Our regex detector catches obvious injections and misses polite ones
  (the evaluation includes templates that evade it on purpose). The lesson was to make detection a signal and put the
  boundary in deterministic code: scope, policy, approvals, mandate.
- **Using the toolkit safely.** Handing the toolkit's `create_order` / `pay_order` straight to the model would bypass every
  control. We call it only behind the mandate check. Its Python OpenAI adapter also pins heavy frameworks, so we used its
  shared layer with the same action filter.
- **No sandbox credentials yet.** So that judges can run everything without accounts, we invested in a faithful offline
  emulation and kept the real-sandbox path behind one flag.

## Accomplishments that we're proud of
- In our red-team scenario, a fully compromised agent obeys a hidden "SYSTEM NOTICE" in a supplier listing. It tries to
  approve itself, buy gift cards, switch to the attacker's store and order 400 units, and every attempt is blocked and
  logged, while the employee's legitimate order still goes through. When we gave a real LLM the same poisoned catalog, it
  noticed the injection but still went for the cheaper attacker quote; the quarantine stopped it (single run, see the
  failure log).
- On 200 synthetic purchase requests (seed 20261110, scripted worst-case agent), full guardrails stopped 94.8% (91/96)
  of attack cases and 95.1% (97/102) of unauthorized attempts, versus 62.5% (60/96) and 64.7% (66/102) with a classic
  spend policy and 0% with none. No normal request was blocked (0/74 in every configuration), and all 30
  delivery-mismatch cases were handled: a dispute for the exact shortfall, or a review flag (see `docs/comparison_200.md`
  for sample composition and denominators). The 5 misses are all the same pattern: an injected listing that evades the
  detector and only asks the agent to buy the *requested* item from the attacker's store. We list this gap openly in
  `docs/failure_cases.md`.
- The whole loop of authorize, execute, reconcile and recover runs end to end, and every decision is verifiable after the fact.

## What we learned
- The useful split is "LLM proposes, code disposes." The model is great at understanding a request and explaining a choice.
  It should never be the thing that decides whether money moves.
- Mandates are most valuable when they are checked twice: before calling the payment API, and against what the payment
  provider actually recorded.
- Post-purchase work (reconciliation, disputes) is where small businesses quietly lose money. An agent is a good fit for
  it because the facts are structured.

## What's next
- Run the full flow on the PayPal sandbox with real buyer/merchant test accounts, and replace the emulated dispute filing
  with the Resolution Center flow.
- Issue mandates from a real AP2-compatible credential provider instead of a local key.
- Package tracking via Orders v2 `track` to time the delivery check automatically.
- Multi-line orders, vendor onboarding, and accounting exports from the audit log.

## Built with
Python · PayPal Orders v2 · PayPal Disputes v1 · PayPal Agent Toolkit · AP2 (Intent Mandate) · Ed25519 · OpenAI-compatible LLM API · vanilla JS
