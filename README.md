# Avengers: Doomsday Ticket Bot → Telegram

Checks **Palaxi Cinemas, HiLITE Mall, Thrissur** on BookMyShow every 10 minutes, for **17 Dec onwards**, and messages you on **Telegram** the moment Avengers: Doomsday shows appear. It runs **free on GitHub Actions**, so your computer can stay off.

| Telegram message | When |
|---|---|
| ✅ *Ticket bot test* | When you run the test |
| 🤖 *Ticket bot is live!* | First real check after deploying |
| 🍿 *Heads-up: booking opened in Thrissur* | Other Thrissur theatres open, Palaxi not yet |
| 🎟️ *TICKETS ARE LIVE!* + screenshot, show times, link | Palaxi HiLITE Thrissur lists the movie (one message per date) |
| 🤖 *Still watching* | Every day at 9 AM |
| ⚠️ *Can't read BookMyShow* | If the site blocks it 3 checks in a row |

It only reads Palaxi's own show listings, not the "upcoming movies" footer that BookMyShow shows on every page, so an upcoming listing won't cause a false alarm. It never books or pays — you book from the link.

---

## Deploy on GitHub (about 10 minutes, one time)

### 1. Your Telegram bot
You've already made it with @BotFather and have its **token**. Make sure you've opened the bot's chat and pressed **Start** — a bot can only message people who have started it.

### 2. Create the repository
1. Sign up / log in at **github.com**.
2. Top-right **+** → **New repository** → name `ticket-bot` → choose **Public** → **Create repository**.
   (Public is what makes it free. Your token goes into Secrets, which nobody can see. It is *not* in any file.)

### 3. Upload the files
1. On the new repo page click **uploading an existing file**.
2. Drag in from the unzipped folder: `bot.py`, `config.json`, `requirements.txt`, `README.md`, `.gitignore` → **Commit changes**.
3. Add the schedule file: **Add file → Create new file**.
   - Name box, type exactly: `.github/workflows/ticket-bot.yml`
   - Open that file from the zip in Notepad/TextEdit, copy everything, paste it in → **Commit changes**.

   *(Can't see the `.github` folder? It's hidden. Windows: View → Show → Hidden items. Mac: Cmd+Shift+. in Finder.)*

### 4. Add your token as a secret
Repo → **Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | your bot token from BotFather |

That's the only one you need. The bot finds your chat by itself.

### 5. Test it, then switch it on
1. Open the **Actions** tab. If asked, click *"I understand my workflows, go ahead and enable them"*.
2. Click **Avengers Doomsday ticket bot** → **Run workflow** → tick **Only send a test Telegram message** → **Run workflow**.
   Within ~2 minutes you get **"✅ Ticket bot test"** on Telegram.
3. **Run workflow** again *without* the tick. That's a real check, and you get **"🤖 Ticket bot is live!"**.
4. To see what it checked: click the run → **check** → **Check for tickets**. You'll see lines like
   `Palaxi Thrissur Thu 17 Dec -> no shows listed yet`

**Done.** From now on it runs by itself every ~10 minutes.

**Test didn't arrive?** Open the run's log. "No Telegram chat found" means you haven't pressed **Start** in your bot's chat. Do that, then run the test again.

---

## Good to know

- **Timing:** GitHub sometimes starts scheduled runs a few minutes late, or skips one when busy. Expect a check every 10–20 minutes.
- **Its memory:** the bot saves what it has already told you in `state.json`, so you don't get repeats. You'll see "Update bot state" commits — that's normal, and the daily one stops GitHub pausing the schedule for inactivity.
- **Want a reminder every 10 min once tickets are live?** Edit `config.json` in the repo (pencil icon) and set `"repeat_alert_minutes": 10`.
- **More dates:** `"days_to_check": 5` checks 17–21 Dec. Raise it, or add dates like `"extra_dates": ["20261225"]`.
- **Blocked warning:** if you get *"⚠️ Can't read BookMyShow"*, BookMyShow is probably blocking GitHub's servers. Run it on your own PC instead (below).
- **Get alerts again:** delete `state.json` in the repo (open it → ⋯ → Delete file).
- **Stop the bot:** Actions → Avengers Doomsday ticket bot → ⋯ → **Disable workflow**.
- **Token safety:** if you ever think your bot token leaked, send `/revoke` to @BotFather, then update the `TELEGRAM_BOT_TOKEN` secret.

## Running on your own PC instead (backup)
1. Install Python 3.9+ ([python.org](https://www.python.org/downloads/), tick "Add Python to PATH").
2. In this folder: `pip install -r requirements.txt` then `python -m playwright install chromium`
3. In `config.json` replace the `bot_token` text with your token.
4. `python bot.py --test-telegram`, then `python bot.py` (checks every 10 min; keep the PC awake).
5. Auto-start: **Windows** Task Scheduler → Create Basic Task → *When I log on* → program `python`, argument `bot.py`, "Start in" = this folder. **Mac/Linux** `crontab -e` → `@reboot cd /path/to/ticket-bot && python3 bot.py`

Run only one copy at a time (GitHub *or* PC), or you'll get double messages.
