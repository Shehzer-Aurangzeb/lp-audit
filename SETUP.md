# lp-audit setup (Windows)

About 45 minutes, once per laptop. You need admin rights for steps 1-2.
Do the steps in order. After each step there is a check: do not go on until it works.

## 1. Check Windows

1. Press `Win + R`, type `winver`, press Enter. It must say **Version 22H2**. If not: Windows Update first.
2. Open Task Manager > Performance > CPU. **Virtualization** must say **Enabled**. If not: restart, open BIOS, turn on **Intel VT-x**.
3. You need about **20 GB** free disk space.

## 2. Install the programs

1. **WSL 2:** open PowerShell **as Administrator** and run:
   ```
   wsl --install
   ```
   Restart the laptop.
2. **Docker Desktop:** download from docker.com, install, keep **Use WSL 2** ticked. Start Docker Desktop.
   Check: in PowerShell, `docker run hello-world` prints **Hello from Docker!**
   Then limit Docker's memory so the laptop stays usable (16 GB laptops):
   ```
   notepad $HOME\.wslconfig
   ```
   Put these 2 lines in the file, save, then run `wsl --shutdown` and start Docker Desktop again:
   ```
   [wsl2]
   memory=6GB
   ```
3. **Git:** download from git-scm.com, install with the default options.
   Check: `git --version`
4. **Ollama:** download from ollama.com and install. Then get the model:
   - Laptop with a graphics card (Altamash): `ollama pull qwen2.5:7b`
   - Laptop without a graphics card (Taha): `ollama pull qwen2.5:3b`

   Check: `ollama list` shows the model.
5. **Claude Code** (your Claude plan): install it as shown on claude.com/claude-code and sign in.
   Check: `claude --version`

## 3. Get the tool

You get an invite to the private GitHub repo. Accept it first. Then in PowerShell:

```
cd $HOME
git clone https://github.com/Shehzer-Aurangzeb/lp-audit.git
cd lp-audit
powershell -ExecutionPolicy Bypass -File install.ps1
```

**Close PowerShell and open a new one** (so it finds the new command).
Check: `lp-audit help` shows the list of commands.

## 4. Settings and first build

```
lp-audit setup
```

This creates the file `.env` in the lp-audit folder. Open it (`notepad $HOME\lp-audit\.env`) and fill in:

| Line | What to put |
| --- | --- |
| `OLLAMA_MODEL` | `qwen2.5:7b` or `qwen2.5:3b` (the one you pulled) |
| `GTMETRIX_EMAIL` / `GTMETRIX_PASSWORD` | Your own free GTmetrix account (one account per person) |
| `GOOGLE_API_KEY` | You get this from Shehzar (send privately, never in chat groups) |
| `CF_API_TOKEN` / `CF_ACCOUNT_ID` | You get these from Shehzar |

Save. Run `lp-audit setup` again: it builds the tool (first time about 10 minutes, 2 GB).
Check: it ends with **demo ok**.

## 5. Try it

1. Code check on the demo site (planted mistakes):
   ```
   lp-audit code $HOME\lp-audit\samples\demo-site
   ```
   Open the report (`lp-audit open`). It should find about 18 problems.
2. Speed check on a real page:
   ```
   lp-audit prelaunch https://your-landing-page-url/ --runs 1 --no-gtmetrix
   ```
3. Real users:
   ```
   lp-audit live https://your-landing-page-url/ --runs 1
   ```
   The report has a **Real users** table with Google and Cloudflare numbers.

## 6. Every landing page from now on

Keep each site's code in its own folder on your laptop.

**Before launch**
1. `lp-audit launch https://the-page-url/ --code C:\Sites\my-page`
2. Copy the Claude command at the end of the code report, run it in the site folder (one pass)
3. Fix, run 1 again until it says READY. Last run on the real domain.

**While ads run:** `lp-audit live https://the-page-url/` a few days after start. Then `lp-audit weekly https://the-page-url/` once: it checks every Monday and opens a window only if real visitors got slower.

**Once a month:** `lp-audit learn`: answer `y` for good new rules.

**Share new rules:** rules live in this Git repo. Whoever ran `lp-audit learn` commits and pushes `code_rules.json` and `manual_checklist.json`; the other person runs `lp-audit update`.

## 7. Updates

When Shehzar says there is a new version:

```
lp-audit update
```

## Rules for safety

- Never send `.env` to anyone or upload it. It has our keys.
- Do not upload the `.git` folder or backup files (`.bak`, `.sql`, `.zip`) from a site folder to the server.
- GTmetrix free plan has only 5 tests. The tool uses 1 per `prelaunch`. Use `--no-gtmetrix` for practice runs.

## If something does not work

| Problem | Fix |
| --- | --- |
| `lp-audit` is not recognized | Close and reopen PowerShell. Still not: run `install.ps1` again |
| Docker is not running | Start Docker Desktop, wait until it says running |
| "Ollama skipped" in the output | Start Ollama (it runs in the system tray). Check `OLLAMA_MODEL` in `.env` |
| GTmetrix step failed | Run the GTmetrix test by hand on gtmetrix.com and write the result in the report |
| Laptop is very slow | Close Chrome tabs. Docker uses about 4 GB RAM |
