# Submission checklist: PayPal AI Hackathon (Devpost)

Deadline: 2026-11-12 12:00 PT. Internal cut-off: materials complete by 2026-11-12 06:00 PT, then wait for the team's
final confirmation before anyone clicks Submit. Rules: https://paypalaihackathon.devpost.com/rules

| Requirement (official rules) | Status | Evidence / notes |
|---|---|---|
| Integrates the PayPal Developer platform (sandbox) as a core feature, plus some AI | Partly done | Orders v2 + Disputes v1 client, tested over HTTP against a local fake; real sandbox run pending credentials (`scripts/sandbox_smoke.py`) |
| Text description incl. significant updates made during the submission period | Draft | `docs/devpost.md`, README "Significant updates during submission period (Nov 2026)" |
| Working demo judges can build and run, or a hosted URL | Done (local) | `python3 web/server.py`, `/approvals?demo=1`; hosted URL optional, not deployed |
| Public repository with all source, run instructions, open-source license detectable in "About" | Waiting | MIT `LICENSE` present; repository not public yet (needs the team's approval and account) |
| Demo video under 3 minutes, shows the project running, public on YouTube, no unlicensed music or trademarks | Script ready | `docs/video_script.md` (about 2:45); not recorded or uploaded yet |
| Test account for judges if the demo needs a login | Not needed | Demo has no login |
| Materials in English | Done | README, docs/ |
| No keys in repo or video | Done | `.env`, `*.env`, `.secrets` git-ignored; smoke script masks credentials |
| Final confirmation before Submit | Waiting | Team decision |

## Pre-submit self-check
- [ ] `python3 -m pytest -q` passes on a clean clone.
- [ ] `python3 scripts/sandbox_smoke.py --fake` prints ALL STEPS OK; with credentials, the real run is recorded in the notes.
- [ ] `python3 -m metrics.comparison` regenerates `docs/comparison_200.md` without changes.
- [ ] Every number in the video and Devpost text appears in `docs/comparison_200.md` or `docs/metrics.md`.
- [ ] `git grep -nI -e "sk-" -e "client_secret=" -e "PAYPAL_CLIENT_SECRET=" -- ':!.secrets.example'` finds nothing sensitive.
- [ ] `.dsh-runs/`, `out/`, `.secrets` are absent from the published tree.
