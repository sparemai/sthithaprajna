"""Cash-only Dhan market: quote all members, backfill selected liquid candidates."""
from dataclasses import replace

from sthitaprajna.data.live import LiveMarket
from sthitaprajna.data.sim import INDICES
from sthitaprajna.intraday.universe import Member


class IntradayMarket(LiveMarket):
    def __init__(self, *args, members: list[Member], **kwargs):
        self.members = {m.symbol: m for m in members}
        self.unresolved: list[str] = []
        super().__init__(*args, specs=INDICES, **kwargs)

    def _build_screen(self):
        super()._build_screen()
        self.screen = {k: s for k, s in self.screen.items() if s.symbol in self.members and s.series == "EQ"}
        self.unresolved = sorted(set(self.members) - {s.symbol for s in self.screen.values()})
        if self.unresolved:
            raise ValueError("Dhan master could not resolve all NIFTY 500 members: " + ", ".join(self.unresolved) +
                             ". Refresh the constituent CSV and instrument cache; no partial universe was started.")

    def promote(self, key):
        if not super().promote(key):
            return False
        member = self.members[self.screen[key].symbol]
        self.states[key].spec = replace(self.states[key].spec, sector=member.industry)
        self.instruments[key] = replace(self.instruments[key], sector=member.industry)
        return True

    def screen_inputs(self):
        now = self.clock()
        # Old scanner quotes must not admit candidates while the quote feed is unavailable.
        return [replace(r, sector=self.members[r.symbol].industry) for r in super().screen_inputs()
                if self.screen[r.key].updated and 0 <= (now - self.screen[r.key].updated).total_seconds() < 30]

    def breadth(self):
        rows = self.screen_inputs()
        adv = sum(r.ltp > r.prev_close > 0 for r in rows)
        dec = sum(0 < r.ltp < r.prev_close for r in rows)
        return {"advancers": adv, "decliners": dec, "ad_ratio": adv / dec if dec else float(adv)}

    def _fetch_expiries(self, und):
        pass

    def service_expiries(self):
        pass

    def expiries(self, underlying, count=4):
        return []

    def option_underlyings(self):
        return []

    def chain(self, underlying, expiry=None):
        return 0.0, []

    chain_blocking = chain

    def option_instrument(self, *args, **kwargs):
        raise ValueError("Options are not supported by the Intraday 500 market")

    def describe(self):
        out = super().describe()
        out["label"] = "NIFTY 500 cash stocks · live Dhan data"
        out["scanner"].update(constituents=len(self.members), unresolved=self.unresolved)
        return out
