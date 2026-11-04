"""Discord operator channel (D10). Optional: requires `pip install sthitaprajna[discord]` and, at gate (a),
DISCORD_BOT_TOKEN + DISCORD_OPERATOR_ID in the environment or `.env`. Setup guide: docs/DISCORD_SETUP.md.
Never posts keys, tokens or account numbers.

* Private server, operator only; commands accepted only from DISCORD_OPERATOR_ID; rate limited.
* Channels: #approvals #trades #risk #opportunities #daily-report #system.
* Approval messages carry [Approve] [Deny]; position alerts carry [Close now] [Close 50%] [Trail tighter].
* No command or button can increase risk or raise the gear (shared handler: api/commands.py).
* Commands start with "/" or "!" (`!status` avoids Discord's own slash-command picker).
* The D10 heartbeat tracks the gateway connection, not message delivery (D-024): a failed post is logged, never fatal.
"""
from __future__ import annotations

import asyncio
import math
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from sthitaprajna.api import commands as cmds

CHANNELS = ("approvals", "trades", "risk", "opportunities", "daily-report", "system")
ROUTES = {"entry": "trades", "fill": "trades", "stage": "trades", "exit": "trades", "risk": "risk", "gear": "risk",
          "tier": "risk", "approval": "approvals", "journal": "daily-report", "system": "system", "fault": "system",
          "error": "system", "operator": "system", "regime": "opportunities", "exec": "system"}
PREFIXES = ("/", "!")
MESSAGE_LIMIT = 1900          # Discord caps a message at 2000 characters
PUMP_S = 2.0
HEARTBEAT_S = 2.0
SEND_ERROR_LOG_EVERY_S = 300.0


def route(event: dict[str, Any]) -> str:
    return ROUTES.get(event.get("kind", ""), "system")


def normalize_command(text: str | None) -> str | None:
    """"!status" and "/status" both mean /status; anything else is chat, not a command."""
    t = (text or "").strip()
    if len(t) < 2 or t[0] not in PREFIXES:
        return None
    return "/" + t[1:]


def chunk_lines(lines: list[str], limit: int = MESSAGE_LIMIT) -> list[str]:
    """Packs lines into as few messages as possible, each within Discord's size limit."""
    chunks: list[str] = []
    cur = ""
    for line in lines:
        line = line if len(line) <= limit else line[:limit - 1] + "…"
        if cur and len(cur) + 1 + len(line) > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks


class EventCursor:
    """Follows engine.events by sequence number, so a full (maxlen) deque never stalls forwarding."""

    def __init__(self, engine, from_now: bool = True):
        self.engine = engine
        self.last = engine.event_seq if from_now else 0

    def drain(self) -> tuple[list[dict[str, Any]], int]:
        """New events since the last drain, and how many rotated out of the deque before we saw them."""
        new = [e for e in list(self.engine.events) if e.get("seq", 0) > self.last]
        dropped = 0
        if new:
            dropped = max(0, new[0]["seq"] - self.last - 1)
            self.last = new[-1]["seq"]
        return new, dropped


def gateway_alive(client) -> bool:
    """True while the bot holds a live gateway connection (the D10 heartbeat)."""
    try:
        ws = getattr(client, "ws", None)
        return bool(client.is_ready() and not client.is_closed() and ws is not None and ws.open
                    and math.isfinite(client.latency))
    except Exception:
        return False


def describe_start_failure(exc: BaseException) -> str:
    name = type(exc).__name__
    if name == "LoginFailure":
        why = ("Discord rejected DISCORD_BOT_TOKEN. Copy the token from Developer Portal -> your app -> Bot -> "
               "Reset Token (not the Application ID or the OAuth2 Client Secret) into .env")
    elif name == "PrivilegedIntentsRequired":
        why = ("Discord refused the connection: turn on MESSAGE CONTENT INTENT in Developer Portal -> your app -> "
               "Bot -> Privileged Gateway Intents, then Save Changes")
    else:
        why = f"Discord connection failed: {type(exc).__name__}: {str(exc).rstrip('. ')}"
    return f"{why}. Restart after fixing (or run without --discord). Until then new entries pause (D10)."


def format_approval(a: dict[str, Any]) -> str:
    s = a["summary"]
    if a["kind"] in ("CONFIRM_ADD", "PRESS"):
        return (f"**{a['kind']}** {s['symbol']} -- qty {s['qty']} @ ~{s['price']} | new stop {s['new_stop']} "
                f"| worst case after Rs {s['worst_case_after']:,.0f} <= planned Rs {s['planned_risk']:,.0f} "
                f"| {s['rule']} | gear {s['gear']} | expires in {a['seconds_left']:.0f}s (timeout = skip)")
    cal = "" if s.get("p_calibrated") else " | P(win) UNCALIBRATED"
    return (f"**{a['kind']}** {s['strategy']} {s['symbol']} ({s['exposure']}) grade **{s['grade']}** gear {s['gear']} "
            f"| stage {s['stage']} qty {s['qty']} (full {s['full_qty']}) | entry {s['entry']} stop {s['stop']} "
            f"| Rs at risk {s['risk_rupees']:,.0f} | EV {s['ev_r']:+.2f}R | conf {s['confidence']} | TQS {s['quality']}"
            f"{cal}\n> {s['reason']}\nHarvest: {s['harvest']} | expires in {a['seconds_left']:.0f}s (timeout = skip)")


def format_position(p: dict[str, Any]) -> str:
    return (f"{p['symbol']} {p['stage']} qty {p['qty']} @ {p['avg_entry']} | stop {p['stop']} (broker {p['broker_stop']}) "
            f"| P&L Rs {p['net_pnl']:,.0f} ({p['r_multiple']:+.2f}R)")


@dataclass
class BotHandle:
    client: Any
    tasks: list[asyncio.Task] = field(default_factory=list)   # strong refs: the loop only keeps weak ones

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        try:
            await self.client.close()
        except Exception:
            pass


def _say(engine, kind: str, msg: str) -> None:
    engine._log(kind, msg)
    print(f"[discord] {msg}", file=sys.stderr)


def run_bot(engine, token: str | None = None, operator_id: str | None = None) -> BotHandle | None:
    """Start the bot inside the running event loop. Returns None (and logs why) when not configured."""
    import os
    token = (token or os.environ.get("DISCORD_BOT_TOKEN") or "").strip()
    operator_id = (operator_id or os.environ.get("DISCORD_OPERATOR_ID") or "").strip()
    if not token or not operator_id:
        _say(engine, "system", "Discord not configured (gate a): set DISCORD_BOT_TOKEN and DISCORD_OPERATOR_ID "
                               "in .env (docs/DISCORD_SETUP.md). Alerts stay in the terminal")
        return None
    if not operator_id.isdigit():
        _say(engine, "error", "DISCORD_OPERATOR_ID must be your numeric Discord user ID (Developer Mode -> "
                              "right-click your name -> Copy User ID), not a username. Discord not started")
        return None
    try:
        import discord
    except ImportError:
        _say(engine, "system", "discord.py not installed: pip install -e \".[discord]\". Alerts stay in the terminal")
        return None

    gate = cmds.CommandGate(operator_id=operator_id)
    intents = discord.Intents.default()
    intents.message_content = True            # privileged: must also be enabled in the Developer Portal
    client = discord.Client(intents=intents)
    cursor = EventCursor(engine)
    state: dict[str, Any] = {"channels": {}, "approvals_posted": set(), "send_errors": {}}

    class ApprovalView(discord.ui.View):
        def __init__(self, approval_id: str):
            super().__init__(timeout=90)
            self.approval_id = approval_id

        async def _answer(self, interaction, ok: bool):
            if str(interaction.user.id) != operator_id:
                await interaction.response.send_message("operator only", ephemeral=True)
                return
            r = engine.approve(self.approval_id, ok)
            await interaction.response.send_message(r.get("result") or r.get("error"), ephemeral=True)

        @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
        async def approve(self, interaction, _button):
            await self._answer(interaction, True)

        @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger)
        async def deny(self, interaction, _button):
            await self._answer(interaction, False)

    class PositionView(discord.ui.View):
        def __init__(self, position_id: str):
            super().__init__(timeout=None)
            self.pid = position_id

        async def _do(self, interaction, fn):
            if str(interaction.user.id) != operator_id:
                await interaction.response.send_message("operator only", ephemeral=True)
                return
            r = fn()
            await interaction.response.send_message("done" if r.get("ok") else r.get("error"), ephemeral=True)

        @discord.ui.button(label="Close now", style=discord.ButtonStyle.danger)
        async def close_now(self, interaction, _b):
            await self._do(interaction, lambda: engine.close(self.pid, 1.0))

        @discord.ui.button(label="Close 50%", style=discord.ButtonStyle.secondary)
        async def close_half(self, interaction, _b):
            await self._do(interaction, lambda: engine.close(self.pid, 0.5))

        @discord.ui.button(label="Trail tighter", style=discord.ButtonStyle.primary)
        async def trail(self, interaction, _b):
            await self._do(interaction, lambda: engine.trail_tighter(self.pid))

    async def send(name: str, content: str, view: Any = None) -> bool:
        ch = state["channels"].get(name)
        if ch is None:
            return False
        try:
            if view is None:
                await ch.send(content)
            else:
                await ch.send(content, view=view)
            return True
        except Exception as exc:       # e.g. Forbidden: the bot lacks permission in that channel
            now = time.monotonic()
            if now - state["send_errors"].get(name, -1e9) >= SEND_ERROR_LOG_EVERY_S:
                state["send_errors"][name] = now
                _say(engine, "error", f"Discord: cannot post in #{name} ({type(exc).__name__}: {exc}). Give the bot "
                                      "View Channel + Send Messages there")
            return False

    @client.event
    async def on_ready():
        state["channels"] = {ch.name: ch for guild in client.guilds for ch in guild.text_channels if ch.name in CHANNELS}
        if not client.guilds:
            _say(engine, "error", "Discord: the bot is logged in but not in any server. Invite it with the OAuth2 "
                                  "URL Generator link (docs/DISCORD_SETUP.md step 4)")
            return
        missing = [c for c in CHANNELS if c not in state["channels"]]
        _say(engine, "system", f"Discord connected as {client.user}: {len(state['channels'])}/{len(CHANNELS)} channels"
             + (f"; missing #{', #'.join(missing)} (those alerts stay in the terminal)" if missing else ""))
        await send("system", f"Sthitaprajna connected ({engine.mode.value}, {engine.perf_label.value}). "
                             "Type `!status` or `/status` here. Commands only work from the operator account.")

    @client.event
    async def on_message(message):
        if message.author.bot:
            return
        text = normalize_command(message.content)
        if text is None:
            return
        r = cmds.handle(engine, text, user_id=str(message.author.id), gate=gate)
        for chunk in chunk_lines(r["reply"].splitlines() or ["-"]):
            await message.channel.send(chunk)

    async def pump_once() -> None:
        from sthitaprajna.engine.snapshot import snapshot
        events, dropped = cursor.drain()
        batches: dict[str, list[str]] = {}
        if dropped:
            batches.setdefault("system", []).append(f"({dropped} events arrived faster than Discord allows; "
                                                    "see the terminal log)")
        for ev in events:
            batches.setdefault(route(ev), []).append(f"`{ev['ts'][11:19]}` {ev['msg']}")
        for name, lines in batches.items():
            for chunk in chunk_lines(lines):
                await send(name, chunk)
        for ev in events:
            if ev["kind"] == "fill":
                pid = next((p.id for p in engine.positions.values() if p.instrument.symbol in ev["msg"]), None)
                if pid:
                    await send("trades", format_position(engine.positions[pid].to_dict()), PositionView(pid))
        for a in snapshot(engine, include_watch=False)["approvals"]:
            if a["id"] not in state["approvals_posted"] and "approvals" in state["channels"]:
                state["approvals_posted"].add(a["id"])
                await send("approvals", format_approval(a), ApprovalView(a["id"]))

    async def pump():
        await client.wait_until_ready()
        while not client.is_closed():
            try:
                await pump_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:   # the pump must never die silently
                _say(engine, "error", f"Discord pump error: {exc!r}")
            await asyncio.sleep(PUMP_S)

    async def heartbeat():
        while not client.is_closed():
            if gateway_alive(client):
                engine.alert_channel_ok = True
                engine.alert_heartbeat = engine.wall()
                engine._alert_email_sent = False
            else:
                engine.alert_channel_ok = False
            await asyncio.sleep(HEARTBEAT_S)

    def on_client_done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            engine.alert_channel_ok = False
            _say(engine, "error", describe_start_failure(exc))

    engine.alert_channel_required = True      # from now on a silent Discord pauses new entries (D10)
    engine.alert_channel_ok = False
    engine.alert_heartbeat = engine.wall()
    start = asyncio.create_task(client.start(token))
    start.add_done_callback(on_client_done)
    return BotHandle(client, [asyncio.create_task(pump()), asyncio.create_task(heartbeat()), start])
