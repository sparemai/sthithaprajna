"""Discord operator channel (D10) against a stand-in `discord` module (the real service needs a bot token).

Covers the failure modes that matter for money: forwarding must survive the engine's event deque rotating,
a channel the bot cannot post in must not stop the heartbeat, and a bad token or missing intent must be
reported with its fix while new entries stay paused (fail closed).
"""
import asyncio
import datetime as dt
import sys
import types
from collections import deque

import pytest

from sthitaprajna import discord_bot as bot
from sthitaprajna.engine.snapshot import snapshot
from test_engine_e2e import CAL, CFG, DAY, RULES, FakeWall, candidate, push, scripted_engine, step

from sthitaprajna.engine.runtime import Engine


def make_fake_discord(channels=("approvals", "trades", "system"), forbidden=(), login_error=None, guilds=True):
    mod = types.ModuleType("discord")

    class Intents:
        def __init__(self):
            self.message_content = False

        @classmethod
        def default(cls):
            return cls()

    class View:
        def __init__(self, *, timeout=180.0):
            self.timeout = timeout

    def button(**_kw):
        return lambda fn: fn

    class Channel:
        def __init__(self, name, fail):
            self.name, self.fail, self.sent = name, fail, []

        async def send(self, content, view=None):
            if self.fail:
                raise RuntimeError("403 Forbidden (error code: 50013): Missing Permissions")
            self.sent.append((content, view))

    class Client:
        instances = []

        def __init__(self, *, intents):
            self.intents, self.handlers, self.ws, self.latency = intents, {}, None, float("nan")
            self._ready, self._closed, self.user = asyncio.Event(), False, "sthitaprajna-bot#0001"
            self.guilds = [types.SimpleNamespace(text_channels=[Channel(n, n in forbidden) for n in channels])] \
                if guilds else []
            Client.instances.append(self)

        def event(self, fn):
            self.handlers[fn.__name__] = fn
            return fn

        async def start(self, token):
            assert token == "token-value"
            if login_error is not None:
                raise login_error
            self.ws, self.latency = types.SimpleNamespace(open=True), 0.05
            self._ready.set()
            await self.handlers["on_ready"]()
            while not self._closed:
                await asyncio.sleep(0.005)

        async def wait_until_ready(self):
            await self._ready.wait()

        def is_ready(self):
            return self._ready.is_set()

        def is_closed(self):
            return self._closed

        async def close(self):
            self._closed = True

    mod.Intents, mod.Client = Intents, Client
    mod.ButtonStyle = types.SimpleNamespace(success=1, danger=2, secondary=3, primary=4)
    mod.ui = types.SimpleNamespace(View=View, button=button)
    return mod


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(bot, "PUMP_S", 0.01)
    monkeypatch.setattr(bot, "HEARTBEAT_S", 0.01)


def small_engine(wall=None) -> Engine:
    kw = {"wall_clock": wall} if wall else {}
    return Engine(CFG, RULES, CAL, seed=3, start=DAY, warmup_days=1, **kw)


def sent(ch):
    return "\n".join(c for c, _ in ch.sent)


# ---------------------------------------------------------------- pure helpers
def test_command_prefixes():
    assert bot.normalize_command("/status") == "/status"
    assert bot.normalize_command("  !target month 8% ") == "/target month 8%"
    assert bot.normalize_command("hello") is None
    assert bot.normalize_command("!") is None and bot.normalize_command(None) is None


def test_chunk_lines_respects_discord_limit_and_keeps_order():
    lines = [f"line {i} " + "x" * (i % 90) for i in range(400)] + ["y" * 5000]
    chunks = bot.chunk_lines(lines)
    assert all(len(c) <= bot.MESSAGE_LIMIT for c in chunks)
    joined = "\n".join(chunks).split("\n")
    assert joined[:400] == lines[:400] and joined[400].startswith("y") and len(chunks) < 40


def test_event_cursor_survives_deque_rotation():
    eng = types.SimpleNamespace(events=deque(maxlen=5), event_seq=0)

    def log(n):
        for _ in range(n):
            eng.event_seq += 1
            eng.events.append({"seq": eng.event_seq, "kind": "system", "msg": str(eng.event_seq)})

    log(2)
    cur = bot.EventCursor(eng)                    # starts "from now": no backlog flood
    assert cur.drain() == ([], 0)
    log(3)
    new, dropped = cur.drain()
    assert [e["seq"] for e in new] == [3, 4, 5] and dropped == 0
    log(12)                                       # the deque holds only the last 5
    new, dropped = cur.drain()
    assert [e["seq"] for e in new] == [13, 14, 15, 16, 17] and dropped == 7
    log(1)
    assert [e["seq"] for e in cur.drain()[0]] == [18]


def test_engine_events_carry_increasing_sequence_numbers():
    e = small_engine()
    seqs = [ev["seq"] for ev in e.events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs) and seqs[-1] == e.event_seq


def test_start_failures_name_the_fix():
    discord = pytest.importorskip("discord")
    assert "Reset Token" in bot.describe_start_failure(discord.LoginFailure("Improper token has been passed."))
    assert "MESSAGE CONTENT INTENT" in bot.describe_start_failure(discord.PrivilegedIntentsRequired(None))
    assert "pause" in bot.describe_start_failure(OSError("network down"))


# ---------------------------------------------------------------- configuration gates
def test_not_configured_or_bad_operator_id_does_not_start(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_OPERATOR_ID", raising=False)
    e = small_engine()
    assert bot.run_bot(e) is None
    assert bot.run_bot(e, token="token-value", operator_id="mahendra#1234") is None
    assert "numeric Discord user ID" in e.events[-1]["msg"]
    assert e.alert_channel_required is False       # nothing started, so nothing to pause on


# ---------------------------------------------------------------- end to end with the stand-in client
def test_bot_batches_events_past_deque_capacity_and_answers_commands(monkeypatch, fast):
    fake = make_fake_discord()
    monkeypatch.setitem(sys.modules, "discord", fake)
    e = small_engine()

    async def main():
        handle = bot.run_bot(e, token="token-value", operator_id="42")
        await asyncio.sleep(0.1)
        client = fake.Client.instances[-1]
        assert client.intents.message_content is True
        ch = {c.name: c for c in client.guilds[0].text_channels}
        assert "Type `!status`" in sent(ch["system"])
        assert any("3/6 channels" in ev["msg"] and "missing #risk" in ev["msg"] for ev in e.events)
        hb_connected = e.alert_heartbeat
        for i in range(450):                      # more than the engine keeps (deque maxlen 400)
            e._log("exit" if i % 2 else "system", f"event {i}")
        await asyncio.sleep(0.1)
        e._log("exit", "after the deque rotated")
        await asyncio.sleep(0.1)

        msg = types.SimpleNamespace(author=types.SimpleNamespace(bot=False, id=42), content="!status",
                                    channel=ch["system"])
        await client.handlers["on_message"](msg)
        msg.author.id, msg.content = 7, "/kill"
        await client.handlers["on_message"](msg)
        msg.author.id, msg.content = 42, "good morning"
        await client.handlers["on_message"](msg)
        await handle.stop()
        return ch, hb_connected

    ch, hb_connected = asyncio.run(main())
    trades, system = sent(ch["trades"]), sent(ch["system"])
    assert "event 449" in trades and "after the deque rotated" in trades
    assert "event 1" not in trades.split("\n")    # rotated out before the pump saw it...
    assert "50 events arrived faster than Discord allows" in system   # ...and said so
    assert len(ch["trades"].sent) < 15            # batched: 226 lines, a handful of messages
    assert all(len(c) <= bot.MESSAGE_LIMIT for chan in ch.values() for c, _ in chan.sent)
    assert "Gear" in system and "Equity" in system
    assert "accepted only from the operator" in system and not e.kill_switch
    assert "good morning" not in system
    assert e.alert_heartbeat > hb_connected and e.alert_channel_ok


def test_channel_without_permission_is_reported_once_and_heartbeat_continues(monkeypatch, fast):
    fake = make_fake_discord(forbidden=("trades",))
    monkeypatch.setitem(sys.modules, "discord", fake)
    e = small_engine()

    async def main():
        handle = bot.run_bot(e, token="token-value", operator_id="42")
        await asyncio.sleep(0.05)
        for i in range(3):
            e._log("exit", f"exit {i}")
            e._log("system", f"sys {i}")
            await asyncio.sleep(0.05)
        hb = e.alert_heartbeat
        await asyncio.sleep(0.05)
        await handle.stop()
        return {c.name: c for c in fake.Client.instances[-1].guilds[0].text_channels}, hb

    ch, hb = asyncio.run(main())
    errors = [ev for ev in e.events if "cannot post in #trades" in ev["msg"]]
    assert len(errors) == 1 and "Send Messages" in errors[0]["msg"]
    assert "sys 2" in sent(ch["system"])          # the other channels keep working
    assert e.alert_channel_ok and e.alert_heartbeat >= hb


def test_bad_token_is_explained_and_new_entries_pause_after_two_minutes(monkeypatch, fast):
    class LoginFailure(Exception):
        pass

    fake = make_fake_discord(login_error=LoginFailure("Improper token has been passed."))
    monkeypatch.setitem(sys.modules, "discord", fake)
    wall = FakeWall()
    e = small_engine(wall)

    async def main():
        handle = bot.run_bot(e, token="token-value", operator_id="42")
        await asyncio.sleep(0.05)
        await handle.stop()

    asyncio.run(main())
    assert any("Reset Token" in ev["msg"] for ev in e.events)
    assert not any("token-value" in ev["msg"] for ev in e.events)   # the token itself is never logged
    assert e.alert_channel_ok is False
    assert "alert channel (Discord) unreachable" not in e._risk_ctx().operational_halts
    wall.t += 121
    assert "alert channel (Discord) unreachable" in e._risk_ctx().operational_halts


def test_bot_not_invited_to_any_server_says_how_to_fix(monkeypatch, fast):
    fake = make_fake_discord(guilds=False)
    monkeypatch.setitem(sys.modules, "discord", fake)
    e = small_engine()

    async def main():
        handle = bot.run_bot(e, token="token-value", operator_id="42")
        await asyncio.sleep(0.05)
        await handle.stop()

    asyncio.run(main())
    assert any("not in any server" in ev["msg"] for ev in e.events)


# ---------------------------------------------------------------- message formats on real engine data
def test_approval_and_position_messages_render_from_real_engine_data():
    e, _wall = scripted_engine(pilot=True)
    key = "NSE:RELIANCE"
    push(e, candidate(e, key))
    entry = snapshot(e, include_watch=False)["approvals"][0]
    text = bot.format_approval(entry)
    assert text.startswith("**ENTRY**") and "timeout = skip" in text and len(text) <= 2000
    assert e.approve(entry["id"], True)["ok"]
    step(e)
    p = next(iter(e.positions.values()))
    assert "RELIANCE" in bot.format_position(p.to_dict())
    e.market.set_price(key, p.avg_entry + 0.6 * p.unit_risk)
    step(e, 30, 2)
    add = next(a for a in snapshot(e, include_watch=False)["approvals"] if a["kind"] == "CONFIRM_ADD")
    assert bot.format_approval(add).startswith("**CONFIRM_ADD**")
    assert dt.datetime.fromisoformat(e.events[-1]["ts"]).tzinfo is not None


def test_real_discord_library_builds_the_bot_and_reports_a_bad_token(monkeypatch, fast):
    """Uses the installed discord.py (skipped when absent): real Intents, real View/button classes, real exception."""
    discord = pytest.importorskip("discord")

    async def bad_login(self, token, *, reconnect=True):
        raise discord.LoginFailure("Improper token has been passed.")

    monkeypatch.setattr(discord.Client, "start", bad_login)
    e = small_engine()

    async def main():
        handle = bot.run_bot(e, token="token-value", operator_id="42")
        await asyncio.sleep(0.05)
        assert handle.client.intents.message_content is True
        await handle.stop()

    asyncio.run(main())
    assert any("Reset Token" in ev["msg"] for ev in e.events)
