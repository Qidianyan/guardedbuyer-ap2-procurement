# Champion-level acceptance checklist (2026-11-10)

Mapped to the five equally weighted criteria in the official rules §6 (see `docs/RECON.md`). An item is ticked only if
the evidence exists in this repo or has been run. Items that need the team are marked **[team]**.

## Stage One (pass/fail gate)
- [x] Uses PayPal developer platform centrally: Orders v2 + Disputes v1, real sandbox run (`docs/sandbox.md`, 2026-11-10)
- [x] Uses AI meaningfully: LLM tool-calling agent + parsing/recommendation/dispute note (`procure/ai.py`, `procure/tool_agent.py`)
- [x] Working build with run instructions (README "Run it"; `python3 web/server.py`; no keys needed)
- [x] Open-source licence file (MIT `LICENSE`) — [ ] **[team]** public repo so GitHub's About section shows it
- [x] Video under 3 minutes, showing the product running (auto-built `materials/demo_video.mp4`) — [ ] **[team]** upload to YouTube as public
- [x] English materials; no secrets in repo/video (`git grep` scan + secrets loaded from outside the repo)

## 1. Technological Implementation
- [x] Real PayPal sandbox: OAuth, create, read-back, not-approved path, approval, capture, safe double capture (`scripts/sandbox_smoke.py --test-card`, all OK)
- [x] End-to-end business flow on the sandbox from the approval desk (as shown in the demo video: order `1S38017531849904R`, capture `8T206088RF006424G`)
- [x] Idempotency per PayPal guidance (unique `PayPal-Request-Id` per call type; POST retried only with an ID)
- [x] Fault handling tested over HTTP: 5xx, 429 + Retry-After, dropped connection after processing, token expiry (`tests/test_paypal_rest_http.py`, 18 tests)
- [x] Disputes v1: list/get/provide-evidence (multipart), sandbox require-evidence
- [x] PayPal Agent Toolkit backend (`--toolkit`), kept away from the LLM by design
- [x] AP2 IntentMandate fields, Ed25519 signature, verified before and after the PayPal call
- [x] Test suite green: 138 passed / 3 skipped (toolkit-optional), plus independent test tasks per feature
- [ ] Webhooks (CHECKOUT.ORDER.APPROVED / CUSTOMER.DISPUTE.*) — not done; listed in What's next

## 2. Design (complete product experience)
- [x] Dashboard, one-page approval desk, AG Grid evaluation explorer, simulated + real PayPal checkout paths
- [x] Consistent visual system (PayPal-blue palette, Lucide icons, no emoji); desktop 1440/1600 and mobile 390 checked
- [x] Every state explained in-page (why blocked, what will be signed, what PayPal returned)
- [x] Accessibility basics: skip link, aria labels, focus-visible, textContent-only rendering
- [x] Screenshots `docs/screenshots/01–11`

## 3. Potential Impact
- [x] Specific audience and problem: small-business purchasing (over-spend + unclaimed short deliveries)
- [x] Evidence the solution addresses it: 200-request comparison, 91/96 vs 60/96 vs 0/96 attack cases, 0/74 false blocks, 30/30 mismatches
- [x] Honest limits (synthetic data, scripted worst case, 5 known misses in `docs/failure_cases.md`)
- [ ] **[team]** one line of real-user validation (e.g., a quote from an office manager) — optional, not fabricated

## 4. Innovation / Idea
- [x] Buyer-side agentic commerce (PayPal's own tools target merchants)
- [x] Human approval turns into a cryptographic AP2 mandate that PayPal capture is checked against
- [x] Agent handles the post-purchase loop (reconcile → dispute draft → evidence)

## 5. Presentation
- [x] Video opens with problem and audience, shows PayPal + AI end to end, no setup screens, AI voice-over + burned-in captions (Devpost tips 2026-11-02)
- [x] At least one segment on the real PayPal sandbox with a visible sandbox order ID
- [x] README top: what, who, run in 3 commands, numbers with denominators, Nov 2026 significant-updates section
- [x] Devpost text for every field (`materials/devpost_submission.md`), pitch deck PDF (`materials/pitch_deck.pdf`)
- [ ] **[team]** YouTube upload (public), Devpost form fill, teammates accepted, final Submit after Modou confirms

## Sponsor prize
- [x] AG Grid Community used for the evaluation explorer (select AG Grid on the form)
- [x] Render Blueprint prepared (`render.yaml`) — [ ] **[team]** deploy only if Modou approves an account
