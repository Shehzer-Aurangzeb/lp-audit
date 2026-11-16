# lp-audit

Speed and code check for our landing pages. It tests a page the way an ad visitor sees it, checks the code before launch, and writes a short report in simple English: what is wrong, how to fix it, how to check the fix.

Rules decide what is broken. AI (Ollama) only writes the summary. One Claude Code pass per code audit finds what the rules missed, and `lp-audit learn` turns those findings into new rules, so the tool gets better with every site.

**First time:** follow [SETUP.md](SETUP.md).
**All commands:** `lp-audit help`.

## How to use it for every landing page

Before launch:

```
lp-audit code C:\Sites\my-page --url https://staging-or-live-url/    # code check, 30 s
# run the Claude command printed at the end of the report (one pass)
lp-audit learn                                                       # Claude's new findings become rules
lp-audit prelaunch https://the-page-url/                             # speed test + GTmetrix once, 4 min
```

Fix the problems. Run `code` and `prelaunch` again until they pass. Do the last `prelaunch` on the real domain, because the Cloudflare cache check only works there.

While ads are running (a few days after start, then every week or after any change):

```
lp-audit live https://the-page-url/
```

Reports: `reports/<domain>/<date>_<mode>/report.md` in this folder (`lp-audit open` opens it).

## What each check does

| Command | Checks |
| --- | --- |
| `prelaunch` | Lighthouse 3x on phone and desktop (on Google's servers in the USA when `GOOGLE_API_KEY` is set), real-visitor test (waits 15 s, scrolls, finds what jumps and what keeps loading), server checks (backend time, Cloudflare cache, the ad-click URL with `?gclid=` and `utm_`, private files like `.git` or `.env` downloadable from the server), GTmetrix 1x |
| `live` | Same as prelaunch (GTmetrix only with `--gtmetrix`), plus real users: Google Chrome data (28 days) and Cloudflare Web Analytics (7 days) |
| `code` | Our rules (`code_rules.json`: debug output, passwords in code, SQL injection, unescaped input, forms without CSRF token, test content...), php -l, PHPStan, Semgrep, gitleaks, files that must not be uploaded. With `--url`: speed problems linked to the exact code line, and live CSS/JS compared with your folder. Saves the code version with git in `reports/.snapshots/` (never inside the site folder, nothing leaves the laptop) |
| `learn` | Checks each rule Claude proposes (catches its bad example, not the fixed one, not a duplicate, not too many hits) and adds the ones you approve |
| `rules` | How often each rule fired, false alarms, export/import to share rules with the team |

Try the code check on the demo site with planted mistakes: `lp-audit code samples/demo-site`.

## Files

| File | What it is |
| --- | --- |
| `audit.py` | The tool (one file) |
| `rules.json` | Speed rules: limits and fix text |
| `code_rules.json` | Code rules. `lp-audit learn` adds to it |
| `manual_checklist.json` | Checks that cannot be automated, shown in every code report |
| `bin/` | The `lp-audit` command (Mac and Windows) and the help text |
| `.env` | Your keys. Never commit or share it |

## Limits

- GTmetrix free plan: 5 tests per account, one account per person. The tool runs it once per launch and skips it when no tests are left.
- Google real-user data needs enough Chrome visitors. New pages show whole-site data first.
- Backend time is server work only (like GTmetrix 'Backend Duration'), so it does not depend much on where you test from.
- Raw test data is deleted after 30 days. Reports stay. About 3-5 MB per audit.
