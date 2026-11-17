# lp-audit

Speed and code check for our landing pages. It tests a page the way an ad visitor sees it, checks the code before launch, and writes a short report in simple English: what is wrong, how to fix it, how to check the fix.

Rules decide what is broken. AI (Ollama) only writes the summary. One Claude Code pass per code audit finds what the rules missed, and `lp-audit learn` turns those findings into new rules, so the tool gets better with every site.

**First time:** follow [SETUP.md](SETUP.md).
**All commands:** `lp-audit help`.

## How to use it for every landing page

Before launch, one command:

```
lp-audit launch https://the-page-url/ --code C:\Sites\my-page
```

It runs the code check, then the speed test, the form and tracking test and the Google Ads basics, and ends with **READY** or **NOT READY**. Run the Claude command printed at the end of the code report once per site. Fix the problems and run `launch` again until it says READY. Do the last run on the real domain, because the Cloudflare cache check only works there. GTmetrix runs once, and only when the code has no must-fix problems (free plan: 5 tests).

While ads are running (a few days after start, then after any change):

```
lp-audit live https://the-page-url/
lp-audit weekly https://the-page-url/     # Windows: every Monday; a window opens only if real visitors got slower
```

Once a month: `lp-audit learn` turns Claude's findings into rules. Commit and push `code_rules.json` and `manual_checklist.json`; the others get them with `lp-audit update`.

**Keeping a problem on purpose:** not every fix is worth it (sales may need the chat widget). Each problem in a report shows its id, for example `chat-widget-shift`. Run `lp-audit accept https://the-page-url/ chat-widget-shift --why "sales needs chat"`: the problem moves to "Accepted, not fixed" in every report and the share summary, and no longer makes the page NOT READY. It is per page, and new problems still count. Commit and push `accepted.json` so the team sees it. A false alarm on one code line is different: put the comment `lp-audit: ok` on that line.

Each landing page has its own history: "since the last audit" and "changed since the last code audit" compare with the last run of the same page, not just the same domain.

**For the team chat:** every `launch`, `prelaunch` and `live` also makes a dated folder `reports/share/<date> <site> <page> <mode>/` (`lp-audit share` opens it): `1 summary.txt` (result in plain words), `2 pagespeed-phone.png`, `3 pagespeed-desktop.png` (from pagespeed.web.dev), `4 gtmetrix.png` (when GTmetrix ran) and `5 phone-after-15-seconds.jpg`. Post these files, not the technical report. The PageSpeed screenshot is one fresh test by Google, so its score can be a little different from the report's middle of 3 tests; the summary gives both.

**When something fails:** the tool never stops silently. A step that did not work is listed under "Steps that did not work" in the report, and the tool prints `Send this file to Shehzar:` with a file in `reports/errors/`. That file has the command, the step, the error and which settings are filled in, but never a key, token or password.

Reports: `reports/<domain>/<date>_<mode>/report.md` in this folder (`lp-audit open` opens it). Each report starts with **In short**: the verdict, the main cause, and what changed since the last audit (fixed / new / still failing). Problems with one cause are shown together: on our first real page, 8 of 11 problems were the same chat widget.

## What each check does

| Command | Checks |
| --- | --- |
| `launch` | `code` + `prelaunch`, then READY / NOT READY (must-fix = severity 1 in either) |
| `prelaunch` | Lighthouse 3x on phone and desktop (on Google's servers in the USA when `GOOGLE_API_KEY` is set), real-visitor test (waits 15 s, scrolls, finds what jumps and what keeps loading, phone screenshots at 1 s, 3 s, 15 s), form and tracking test, Google Ads basics, server checks (Cloudflare cache, ad-click URL, response time), GTmetrix 1x |
| `live` | Same as prelaunch (GTmetrix only with `--gtmetrix`), plus real users: Google Chrome data (28 days) and Cloudflare Web Analytics (7 days) |
| `watch` / `weekly` | `live --runs 1 --no-ai`; warns only when Cloudflare's 7-day phone or desktop LCP, INP or CLS is over target and 20% worse than the last live audit (and has 30+ visits) |
| `code` | Our rules (`code_rules.json`: debug output, passwords in code, SQL injection, unescaped input, forms without CSRF token, test content...), php -l, PHPStan, Semgrep, gitleaks, files that must not be uploaded. With `--url`: speed problems linked to the exact code line, and live CSS/JS compared with your folder. Saves the code version with local git (nothing leaves the laptop). Skips `*.min.js` / `*.min.css`. A line with the comment `lp-audit: ok` is not reported |
| `learn` | Checks each rule Claude proposes (catches its bad example, not the fixed one, not a duplicate, not too many hits) and adds the ones you approve |
| `rules` | How often each rule fired or was silenced with `lp-audit: ok` |

**Form and tracking test.** A phone browser opens the ad-click URL, fills the visible form fields with test data (honeypot fields stay empty), and presses Submit. It records Google Tag Manager, gtag.js, Google Ads conversion and remarketing, GA4 and the Meta pixel. Every tracking request is blocked after it is seen, so nothing is counted in Google Ads or Meta. By default the form request is blocked too, so no lead is created. `--submit` sends it for real (use on staging) and checks the thank-you page; `--thank-you <url>` opens the thank-you page to check the conversion there. Fails when: no Google tag, the conversion fires when the page opens, no conversion after Submit, the form sends nothing, or a PHP error / 404 after Submit.

**Google Ads basics.** robots.txt must not block AdsBot (it ignores `User-agent: *`), links and images must not be broken (404/410/5xx), and a few Lighthouse checks from the SEO, best-practices and accessibility categories (viewport, font size, https, console errors, form labels, contrast). The AdsBot fetch from the laptop is shown as a hint only: Cloudflare checks real bots by IP.

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

- GTmetrix free plan: 5 tests per account, one account per person. The tool runs it once per launch and skips it when no tests are left. If the auto-login fails, run the test by hand and pass the report link with `--gtmetrix-url`.
- Google real-user data needs enough Chrome visitors. New pages show whole-site data first.
- Backend time is server work only (like GTmetrix 'Backend Duration'), so it does not depend much on where you test from.
- Raw test data is deleted after 30 days. Reports stay. About 3-5 MB per audit.
- The form test uses fake data that fits most forms. A CAPTCHA or a strict phone format can stop it; the report then says so and you test one lead by hand.
- `weekly` is Windows only (Task Scheduler). Docker Desktop must be running on Monday; if it is not, a window says so.
