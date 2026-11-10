# Demo video script: GuardedBuyer (target 2:45, hard limit under 3:00)

> The submitted cut is built automatically by `scripts/video/make_demo_video.py` (v2, 2:52). Its exact narration is
> `materials/demo_video_narration.txt`, and the sandbox IDs it shows are in `materials/demo_video_ids.json`. The manual
> recording plan below is the fallback.

**Recording setup.** `python3 web/server.py --port 8000` (offline PayPal emulation; add `--sandbox` once sandbox
credentials are in `.secrets`, see `docs/sandbox.md`). Chrome at 1440×900, zoom 100%, a terminal window at the same size.
Before each take open `http://127.0.0.1:8000/approvals?demo=1` in a fresh tab (it resets the demo and loads the blocked
laptop request). Voice-over (VO) in plain English, no music (or royalty-free only). Everything on screen is synthetic
demo data; keep the "Offline PayPal emulation" chip and the "SIMULATED PayPal sandbox checkout" banner visible, and never
call the emulation live PayPal. If the take is recorded with `--sandbox`, say "PayPal sandbox" instead.

| Time | Screen | VO / caption |
|---|---|---|
| **0:00–0:12** | **Problem.** Title card. Captions: "AI agents can now pay." → "One poisoned listing can redirect your money." → "Short deliveries rarely get claimed back." | "AI agents can now shop and pay for a business. One line hidden in a supplier listing can hijack them, and when a delivery comes up short, nobody claims the money back." |
| 0:12–0:22 | Dashboard `/`, cursor circles the main panels. | "GuardedBuyer is a buyer-side procurement agent built on PayPal and AP2 mandates. The AI proposes; deterministic code decides whether money moves." |
| **0:22–0:45** | **Over-authority blocked.** Approval desk `/approvals?demo=1`. Red banner "Blocked by policy"; zoom on panel 1: reasons, budget bar "Over budget by $700.00", "Agent tried to pay: refused". | "An employee asks for two laptops. That is $2,200 against $1,500 left in the IT budget. The agent tries to pay anyway, and the payment is refused and logged. The agent has no approve tool at all." |
| 0:45–1:00 | Panel 2 (request, chosen quote, reason) and panel 4 "Constraints that will be signed": merchant, SKU, max $2,200, quantity 2. Panel 6 audit table: `purchase_blocked`, `execution_refused` in red. | "The approver sees the request, why this supplier was picked, and the exact limits that will be signed. Each event is in a hash-chained audit log." |
| **1:00–1:25** | **Human release.** Type a note "Budget moved from Q1 onboarding", click "Approve as manager (alice)", then "Approve as finance (bob)". Banner turns blue "Approved, ready to release". Click "Release & place PayPal order". Panel 4 now shows mandate ID, Ed25519 signature, "Signature valid", approvals finance + manager. | "Manager and finance approve, and their notes go into the log. Release signs a new AP2 Intent Mandate that pins merchant, item, quantity and the maximum amount, and verifies it before PayPal is called." |
| 1:25–1:45 | Panel 5: PayPal order `PAYER_ACTION_REQUIRED` → "Open PayPal checkout" → simulated checkout page (banner visible) → "Pay now" → return_url page "Payment captured" → "Back to approval desk": banner green "Paid", capture ID, audit rows `paypal_order_created`, `paypal_order_captured`, chain valid. | "The agent creates a PayPal Orders v2 order. The company's PayPal buyer approves it, PayPal redirects back, and only then do we capture. Before that we read the order back from PayPal and check it against the mandate." |
| **1:45–2:08** | **Delivery mismatch → dispute.** Dashboard `/`, open the paid laptop request (or `?demo=laptop-paid`), Receiving: "received 1 of 2" → Dispute panel: reason MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED, amount $1,100.00, factual message; status tracked. | "One laptop arrives instead of two. The agent reconciles the delivery and drafts the PayPal dispute for exactly the missing $1,100, with a factual message. PayPal has no API for a buyer to open a dispute, so a person files the draft in the Resolution Center and the agent tracks it through the Disputes API." |
| 2:08–2:25 | **Red team.** Dashboard `?replay=all&panel=trace&trace=red_team`: blocked steps in red (self-approval refused, gift cards blocked, attacker store quarantined, 400 units out of scope). | "Now a poisoned listing with a fake system notice. A fully compromised agent obeys it: approve itself, buy gift cards, switch stores, order four hundred units. Every attempt is blocked, and the real order still goes through." |
| 2:25–2:38 | Flash `docs/charts/comparison_200.svg`, then the terminal running `python3 scripts/sandbox_smoke.py --fake` (or the real sandbox run) with `[ok]` lines. | "On 200 synthetic requests, full guardrails stopped 91 of 96 attack cases, against 60 for a classic spend policy, with no legitimate purchase blocked. And the PayPal client is tested for retries, idempotency and the not-approved path." |
| 2:38–2:45 | Closing card: "GuardedBuyer: AP2 mandates + PayPal Orders & Disputes + an agent that can't overspend." Repo link. | "GuardedBuyer: let AI do the buying, without handing it the keys." |

## Numbers allowed in the VO
Only numbers from `docs/comparison_200.md` (generated from `metrics/results_200.json`), and say "synthetic":
- attack cases stopped: full guardrails 91/96 (94.8%), policy only 60/96 (62.5%), no guardrails 0/96;
- unauthorized attempts stopped: 97/102 (95.1%) vs 66/102 (64.7%);
- false blocks on normal requests: 0/74 in every configuration; delivery mismatches handled: 30/30 with full guardrails.
If the case set is regenerated, re-read the numbers from the new file before recording.

## Shot checklist
- Fresh tab on `/approvals?demo=1` for each take (the page resets the demo once per browser tab session).
- Keep the PayPal mode chip and the simulated-checkout banner in frame.
- Do not show `.secrets`, environment variables or terminal history with credentials. The smoke script masks them.
- Total runtime must stay under 3:00 (YouTube shows the length; judges stop at 3:00).
