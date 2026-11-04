# Discord operator channel: setup (D10)

This gets you the two values in `.env`:

```
DISCORD_BOT_TOKEN=      # the bot's password (secret)
DISCORD_OPERATOR_ID=    # YOUR numeric Discord user ID (the only account allowed to command the bot)
```

It takes about 10 minutes and costs nothing. Discord's screens change from time to time, so if a button has moved, look
for the same words nearby.

> **The bot token is a password.** Anyone who has it can act as your bot. Keep it only in `.env`, which git ignores.
> Never commit it, screenshot it or paste it into a chat, including a chat with an AI assistant. If it leaks, use
> **Reset Token** (step 2), which kills the old one immediately.

## 1. Create a private server
1. Open Discord (app or discord.com) → the **+** button at the bottom of the server list → **Create My Own** →
   **For me and my friends** → name it (e.g. `Sthitaprajna`) → **Create**.
2. Create six **text** channels with exactly these names (right-click the server name → **Create Channel**):

   | Channel | What arrives there |
   |---|---|
   | `approvals` | Proposals with **[Approve] [Deny]** buttons (60 s; no answer = skip) |
   | `trades` | Entries, fills, stage changes, exits; position cards with **[Close now] [Close 50%] [Trail tighter]** |
   | `risk` | Gear changes, risk events, tier changes |
   | `opportunities` | Regime changes |
   | `daily-report` | Journal and end-of-day reports |
   | `system` | Start-up, faults, feed problems, and replies to your commands |

   Missing channels are fine: those alerts stay in the web terminal, and the console says which ones are missing.

## 2. Create the bot and copy DISCORD_BOT_TOKEN
1. Go to <https://discord.com/developers/applications> and log in with the same Discord account.
2. **New Application** → name it (e.g. `Sthitaprajna Bot`) → accept the terms → **Create**.
3. Left menu → **Bot**.
4. Click **Reset Token** → confirm (and enter your 2FA code if asked) → **Copy**. This long string is your
   **`DISCORD_BOT_TOKEN`**. Discord shows it only once. If you lose it, reset it again.
   - It is *not* the **Application ID** (General Information) or the **Client Secret** (OAuth2). Those will not log in.

## 3. Turn on the Message Content intent (required)
On the same **Bot** page, under **Privileged Gateway Intents**:
- Turn **ON** → **Message Content Intent** (the bot needs it to read `!status`, `/kill` and the other commands).
- Presence Intent and Server Members Intent can stay off.
- Click **Save Changes**.

Optional but recommended: turn **Public Bot** OFF so that only you can add the bot to servers. If Discord refuses with
an error about an install link, first go to **Installation** → set **Install Link** to **None** → Save, then turn
Public Bot off again.

## 4. Invite the bot to your server
1. Left menu → **OAuth2** → **URL Generator**.
2. **Scopes:** tick **`bot`**.
3. **Bot Permissions:** tick **View Channels**, **Send Messages** and **Read Message History**.
4. Copy the **Generated URL** at the bottom, open it in your browser, pick your private server → **Continue** →
   **Authorize**.
5. The bot now appears (offline) in your server's member list. It comes online when Sthitaprajna starts.

## 5. Copy your DISCORD_OPERATOR_ID
1. Discord → **User Settings** (the gear next to your name) → **Advanced** → turn **Developer Mode** ON.
2. Close settings. Right-click **your own name** (member list, or any message you posted) → **Copy User ID**.
   On a phone, open your profile → **⋯** → **Copy User ID**.
3. This long number (usually 17–19 digits) is your **`DISCORD_OPERATOR_ID`**.
   - Copy **your** ID, not the server's ID and not the bot's. Commands from any other account are refused.

## 6. Put both values in `.env`
In the `Sthitaprajna` folder (the one containing `pyproject.toml`), in Git Bash:

```bash
cp .env.example .env      # only once; skip if .env already exists
code .env                 # or open it in any text editor
```

Fill in the two lines. Don't use quotes, and don't put spaces around the `=`:

```
DISCORD_BOT_TOKEN=paste-the-token-here
DISCORD_OPERATOR_ID=123456789012345678
```

Save the file. On Windows, check that it is really named `.env` and not `.env.txt`: `ls -a` in Git Bash should list `.env`.
Sthitaprajna reads `.env` automatically from the folder you start it in. Variables already set in your shell take
precedence over the file.

## 7. Install the Discord extra and start
```bash
pip install -e ".[dev,discord]"
sthitaprajna serve --discord
```

A healthy start looks like this in the console:

```
Settings loaded from ...\Sthitaprajna\.env: DISCORD_BOT_TOKEN, DISCORD_OPERATOR_ID
Sthitaprajna 2.0 (Options Hero) terminal on http://127.0.0.1:8788  (mode SIM, SIM data is not evidence)
[discord] Discord connected as Sthitaprajna Bot#1234: 6/6 channels
```

`#system` then shows *"Sthitaprajna connected (SIM, SIMULATED). Type `!status` or `/status` here."*

## 8. Test it
Type in `#system`:

| Command | Does |
|---|---|
| `!status` | Gear, equity, day P&L, open risk, positions, process score |
| `!help` | Full command list |
| `!positions`, `!pnl`, `!queue`, `!gear` | Read-only views |
| `!lock day`, `!target month 8%` | Stop new entries today / set a target (targets never change risk) |
| `!close <id>`, `!reduce <id>`, `!trail <id>`, `!close all` | Protect or reduce a position |
| `!kill` | Flatten everything and halt |

`/status` works too. But Discord opens its own slash-command menu when you type `/`, and that menu may run a built-in
command instead of sending your text. The `!` form always reaches the bot. **No command or button can open a
trade, add size, widen a stop or raise the gear.**

## How it behaves once enabled
- **If Discord is unreachable for more than 2 minutes, new entries pause** (D10) until the connection returns. Broker-side
  stops stay in place. With open positions, one email alert is sent if `SMTP_*` is set in `.env`. This is deliberate:
  the system must not keep opening trades while it can't reach you.
- In SIM at 30× speed, alerts are batched into a few messages every 2 seconds. Consider muting `#trades` and
  `#opportunities` and keeping notifications on for `#approvals` and `#risk`.
- To run without Discord, start without `--discord`.

## Troubleshooting (console message → fix)
| Message | Fix |
|---|---|
| `Discord not configured ...` | `.env` is missing, misnamed (`.env.txt`), in a different folder, or a name is misspelled. Start from the folder containing `.env`. |
| `discord.py not installed ...` | `pip install -e ".[discord]"`, using the same Python/venv that runs `sthitaprajna` |
| `DISCORD_OPERATOR_ID must be your numeric Discord user ID ...` | You pasted a username. Repeat step 5. |
| `Discord rejected DISCORD_BOT_TOKEN ...` | Wrong or old token (or the Application ID / Client Secret). Repeat step 2 (Reset Token). |
| `... turn on MESSAGE CONTENT INTENT ...` | Step 3, then **Save Changes** and restart |
| `the bot is logged in but not in any server` | Step 4 (invite link) |
| `3/6 channels; missing #risk, ...` | Create the missing channels (exact names, step 1.2) |
| `cannot post in #trades (Forbidden ...)` | Channel settings → **Permissions** → add the bot → allow **View Channel** and **Send Messages** |
| `commands are accepted only from the operator` | `DISCORD_OPERATOR_ID` is not your ID (maybe the server's or bot's). Repeat step 5. |
| The bot is online but never replies | Message Content intent is off (step 3), the bot can't see that channel, or Discord's `/` menu ate the text. Use `!status`. |
