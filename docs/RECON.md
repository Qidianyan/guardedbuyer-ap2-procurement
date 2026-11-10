# RECON: PayPal AI Hackathon (re-checked 2026-11-10)

All pages below were read on **2026-11-10 (PT)**. Anything we could not confirm is marked *unverified*.

## 1. Official pages (source of truth)

| Page | URL | Read |
|---|---|---|
| Overview | https://paypalaihackathon.devpost.com/ | 2026-11-10 |
| Official rules | https://paypalaihackathon.devpost.com/rules | 2026-11-10 |
| Resources | https://paypalaihackathon.devpost.com/resources | 2026-11-10 |
| Updates | https://paypalaihackathon.devpost.com/updates | 2026-11-10 |
| FAQs | https://paypalaihackathon.devpost.com/details/faqs | 2026-11-10 |
| Discussions | https://paypalaihackathon.devpost.com/forum_topics | 2026-11-10 |
| Project gallery | https://paypalaihackathon.devpost.com/project-gallery | 2026-11-10: not yet published |

### Dates (rules §1)
- Submission period: Oct 1, 2026 9:15 PT → **Thu Nov 12, 2026 12:00 PT** (= Beijing **Fri Nov 13 04:00**).
- Judging: Dec 1 → Dec 15, 2026. Winners announced on or around Dec 21, 2026.
- Overview page shows 33,4xx participants (2026-11-10).

### What to submit (overview "What to Submit" + rules §4)
1. A project that integrates the PayPal developer platform (sandbox is fine) **and** an AI tool/model; PayPal must be central.
2. Text description of features and functionality. For an existing project: explain the significant update made during the submission period.
3. A functional demo: either clear setup/run instructions in the repo **or** a hosted URL. Mockups and static prototypes don't count.
4. **Tools:** name the models/AI services and explain how they were used (overview page lists this as its own item).
5. A public repo on GitHub/GitLab/Bitbucket with all source, assets and instructions, plus an open-source licence visible in the About section.
6. Demo video: under 3 minutes, showing the project running on its target device, public on YouTube, with no unlicensed third-party trademarks or music.
7. Testing: if anything needs login or keys, put test credentials and instructions in the README or the project description.
8. English (or English translations).

### Judging (rules §6)
- **Stage One** is pass/fail: does the project fit the theme and reasonably use the required APIs/SDKs? The 2026-11-02 update says reviewers also check for a working build, a public repo with a licence, and a public video under 3 minutes.
- **Stage Two** scores five equally weighted criteria. Ties are broken in this order:
  1. **Technological Implementation:** how thoroughly and skilfully the project uses PayPal and the AI tools, with a working, non-trivial build.
  2. **Design:** a complete, coherent product experience, not just a technical proof of concept.
  3. **Potential Impact:** a credible, specific case for a real problem and audience, and a demo that actually addresses it.
  4. **Innovation/Idea:** a creative, novel concept that differs from existing ones.
  5. **Presentation:** a video that shows the project working end to end, and a pitch that says what problem is solved, for whom, and why it matters, in an easy-to-follow way.
- Judging may use expert panels, peer review, or "automated AI-driven analysis" (rules §6). The README and description therefore need to be explicit and machine-readable.
- Judges include PayPal Developer Advocates, a PayPal engineering manager and a senior PM, plus one judge each from AG Grid, APIMatic, Bryntum, Channel3, Elastic, Postman and Render.

### Prizes (rules §8)
- **Grand:** $12k / $8k / $5k.
- **Honorable Mentions:** $5k each for Most Creative, Most Impactful, Best Demo Delivery, Best Use of PayPal + AI, and Best Use of Agentic Commerce.
- **Sponsor prizes:**

  | Prize | Award |
  |---|---|
  | AG Grid | $5k / $2k / 3×$1k |
  | APIMatic | 3×$1k + subscription |
  | Bryntum | 3×$1k |
  | Channel3 | $1.5k |
  | Render | $1,000 / $750 / $500 in credits |

- A project can win at most one Grand Prize **or** one Honorable Mention, **plus** one Sponsor Prize.
- Sponsor eligibility in the rules table is simply "All Eligible Submissions that use any <sponsor> tools". The 2026-11-05 update adds: select the sponsor tools on the submission form.

### Updates that change what we do
- **2026-11-09 ("3 days left"):** upload the video today because YouTube processing can take hours. After the deadline, don't touch anything (no commits, README edits or video swaps).
- **2026-11-05 ("Submission checklist"):**
  - Uses PayPal and AI.
  - Hosted URL or run instructions.
  - Public repo with README and licence.
  - Public video under 3 minutes.
  - Sponsor tools selected on the form.
  - Teammates invited and accepted.
  - Keep demo, test credentials and trials active until winners are announced.
- **2026-11-02 (video tips):**
  - Open with the problem and who it's for, then show PayPal and AI working end to end.
  - Cut loading and setup.
  - **An AI voiceover is fine.**
- **2026-10-28 ("How to stand out"):** make PayPal and AI impossible to miss by naming both in the description, "Built With" and the video. Give judges something to run. Never commit secrets.
- **FAQ:**
  - Teammates join through the project's team section, using an invite link.
  - PayPal sign-up availability by country is separate from eligibility; ask in the PayPal Discord.
- **Discussions (titles only, 2026-11-10):** sandbox access from various countries, "API Key for Judges", "PayPal sandbox MCP - Error", and "Are PayPal's own checkout and AI assistant names OK on screen in the demo video?". *Unverified:* we did not see an official answer to the last one. We keep PayPal names on screen only where they are the real API or page being used, and use no PayPal logos.

## 2. Sponsor prizes: can we qualify cheaply?

| Sponsor | What qualifies (rules) | Fit for us | Cost | Decision |
|---|---|---|---|---|
| **AG Grid** | "use any AG Grid tools" | Our evidence is tabular: 200 cases × 3 configurations, plus the approval queue and audit. AG Grid Community is MIT, free, and needs no key (v36.3.0, checked on npm and jsDelivr on 2026-11-10). | Low: one page, vendored JS | **Done.** `/evaluation` uses AG Grid Community for the full case table, with sorting, filters, quick search, pagination and custom outcome cells. Select AG Grid on the form. |
| **Render** | "use any Render tools" | The stdlib server reads `PORT`, so a Blueprint is trivial. | Very low | **Prepared:** `render.yaml` (free plan, offline emulation by default). Not deployed: deploying needs an account and Modou's OK. |
| APIMatic | "use any APIMatic tools" | Would mean regenerating our PayPal client from an APIMatic SDK or context plugin. | Medium, and needs an account | Skip. |
| Bryntum | "use any Bryntum tools" | Gantt and scheduler components; Bryntum is commercial (trial). No natural fit. | Medium | Skip. |
| Channel3 | "use any Channel3 tools" | A product catalogue API could replace our synthetic catalogue, but it needs an account and key, and it makes the demo depend on a live third party. | Medium | Skip for now. |

Only one Sponsor Prize can be won, so AG Grid (largest pool, and its judge Sylwia Vargas reviews grids) is the target. Render stays a free extra.

## 3. PayPal agentic-commerce direction (what judges will be primed on)

| Item | Source (read 2026-11-10) | What it means for us |
|---|---|---|
| PayPal **Agent Toolkit** (Python/TS) and the **MCP server**, local or remote. Sandbox endpoint `https://mcp.sandbox.paypal.com`; quickstart last updated 2026-06-30. | https://developer.paypal.com/ai-tools/mcp-server ; https://developer.paypal.com/community/blog/paypal-model-context-protocol/ | We already run PayPal calls through the official Agent Toolkit backend (`--toolkit`). The MCP server is aimed at merchants; we deliberately do **not** hand payment tools to the LLM. |
| **AP2 (Agent Payments Protocol)**, announced 2025-09-16, with PayPal support. Intent, Cart and Payment mandates; signed, verifiable user intent. | https://developer.paypal.com/community/blog/PayPal-Agent-Payments-Protocol/ | Our core is an AP2 Intent Mandate (human-not-present) with signed spend constraints. The approval desk shows a human release turning into a newly signed mandate. |
| PayPal + Google Cloud agentic commerce for merchants (A2A + AP2), 2025-10-27 | https://cloud.google.com/blog/topics/financial-services/introducing-an-agentic-commerce-solution-for-merchants-from-paypal-and-google-cloud | Merchant side. We stay on the **buyer/business-spend** side, which PayPal's own products don't cover. |
| Hackathon resources "AI Agents or Automation": "not just recommend, but actually transact" | resources page | We transact through Orders v2 create, buyer approval and capture, plus Disputes v1 tracking and evidence. |

## 4. What wins (patterns, not a specific gallery)
This hackathon's gallery is not published yet (checked 2026-11-10), so there are no past winners of this exact event. The patterns below come from Devpost's own guidance for this event (updates and resources) and from winner-badged agent projects on Devpost, which a third-party guide lists (https://hackers.pocketplay.win/en/hackathons/paypalaihackathon/, read 2026-11-10). Treat them as heuristics:
1. **The video does most of the work.** Open with the problem and who it's for, show the real product running end to end, and skip setup. (Devpost update 2026-11-02.)
2. **Narrow scope with a visible human-confirm step** for agents that pay. (Third-party guide's advice on Best Use of Agentic Commerce.)
3. **Real API calls and evidence.** Numbers backed by a reproducible evaluation, plus a README a judge can run in a few minutes.
4. **A complete product experience.** A designed UI rather than a CLI, with consistent visual language.
5. **Sponsor tools that are used meaningfully**, not just a logo.

## 5. Gaps found by this recon → actions
| Gap | Action | Status |
|---|---|---|
| "Tools" must be named and explained | `materials/devpost_submission.md` gets a "Built with / AI tools" section | done |
| Sponsor prize needs real use and a form selection | AG Grid `/evaluation` page; note "select AG Grid" in the checklist | done |
| Upload the video early | Auto-generated cut is ready (`materials/demo_video.mp4`); upload needs Modou | waiting for Modou |
| Real PayPal sandbox run | Waiting for credentials; `scripts/sandbox_smoke.py` is ready | blocked |
| Keep the demo and credentials alive until Dec 21 | Noted in the handoff README | noted |
