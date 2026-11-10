# Pitch deck outline (8 slides, about 4 minutes)

1. **Title**: GuardedBuyer: an AP2-guarded procurement agent for small businesses. One line: "Let AI do the buying without giving it the keys."
2. **Problem**:
   - SMBs buy constantly; one person, one card, one spreadsheet.
   - Agents with payment tools can be hijacked by text in a product listing (prompt injection).
   - Short or wrong deliveries: refunds aren't claimed.
   - Visual: a poisoned listing with the hidden "SYSTEM NOTICE" highlighted.
3. **Insight**: The LLM proposes, code disposes. The model reads, compares and explains. Deterministic layers decide: scope → policy and human approval → AP2 mandate → PayPal read-back → reconciliation. Visual: a layered diagram.
4. **Product demo** (screenshots from `docs/screenshots/`): request → quotes with reason → approval → mandate → PayPal approve/return → capture → delivery check → dispute draft.
5. **Red team**: The compromised-agent timeline with the blocked events in red; the legitimate order still completes. Also the honest note: the real LLM noticed the injection but still chose the attacker quote, and the quarantine stopped it.
6. **Evidence**: `docs/charts/headline.svg`: block rate, false-block rate and dispute coverage across no guardrails / policy only / full. State sample sizes, that the data is synthetic, and the worst-case scripted agent. Mention the known gap (in-scope purchases from an undetected attacker) from `docs/failure_cases.md`.
7. **How it uses PayPal and AP2**: Orders v2 (payer-action approval, capture, read-back), Disputes v1 (track and accept-claim; buyer filing via the Resolution Center), PayPal Agent Toolkit behind the guard, AP2 IntentMandate fields plus our signed spend-limit extension, and the hash-chained audit.
8. **What's next and the ask**: Sandbox end-to-end, a real AP2 credential provider, Orders tracking for automatic delivery checks, accounting export. Ask: feedback from PayPal on buyer-side dispute APIs, and pilot SMBs.
