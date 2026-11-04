"""Aggressive-from-day-1 policies, with gap losses. 20,000 paths x 250 trades (~1 year).
Outcomes: win = +b R; loss = -1R, except 4% of losses gap through the stop at -2.5R.
Ladders map drawdown-from-peak -> risk fraction; reaching the halt level stops trading for the year.
Edge brake: from trade 50 on, R <= max(1.0%, quarter-Kelly estimated from the path's own live trades)."""
import numpy as np

rng = np.random.default_rng(2026)
N, T = 20000, 250
GAP_P, GAP_LOSS = 0.04, 2.5
scen = {"Real edge": (0.45, 1.8), "Weak edge": (0.40, 1.6), "No edge": (0.38, 1.6)}


def base(dd):
    return np.select([dd < .05, dd < .08, dd < .12, dd < .15], [.015, .01125, .0075, .00375], 0.0)


def attack(dd):
    return np.select([dd < .10, dd < .15, dd < .20, dd < .25], [.025, .020, .010, .005], 0.0)


def run(p, b, ladder, brake):
    win = rng.random((N, T)) < p
    gap = rng.random((N, T)) < GAP_P
    out = np.where(win, b, np.where(gap, -GAP_LOSS, -1.0))
    eq = np.ones(N); peak = np.ones(N); halted = np.zeros(N, bool)
    nw = np.zeros(N); sw = np.zeros(N); nl = np.zeros(N); sl = np.zeros(N)
    for t in range(T):
        dd = 1 - eq / peak
        f = ladder(dd)
        halted |= f == 0
        if brake and t >= 50:
            pw = nw / t
            bb = np.where(nl > 0, (sw / np.maximum(nw, 1)) / (sl / np.maximum(nl, 1)), 10)
            kelly = np.where(bb > 0, pw - (1 - pw) / np.where(bb > 0, bb, 1), -1.0)
            f = np.minimum(f, np.maximum(0.010, 0.25 * kelly))
        f = np.where(halted, 0.0, f)
        o = out[:, t]
        eq *= 1 + f * o
        peak = np.maximum(peak, eq)
        traded = ~halted
        nw += (o > 0) & traded; sw += np.where((o > 0) & traded, o, 0)
        nl += (o < 0) & traded; sl += np.where((o < 0) & traded, -o, 0)
    return eq, halted


pol = [("BASE 1.5%, halt 15%", base, False),
       ("ATTACK day 1, halt 25%", attack, False),
       ("ATTACK day 1 + edge brake", attack, True)]
print(f"{'scenario':10} {'policy':28} {'median':>7} {'5th pct':>8} {'P(halt)':>8} {'P(loss)':>8}")
for s, (p, b) in scen.items():
    ev = p * b - (1 - p) * (1 + GAP_P * (GAP_LOSS - 1))
    for name, lad, br in pol:
        eq, h = run(p, b, lad, br)
        print(f"{s:10} {name:28} {np.median(eq):>6.2f}x {np.percentile(eq, 5):>7.2f}x "
              f"{h.mean()*100:>7.1f}% {(eq < 1).mean()*100:>7.1f}%   (EV/trade incl. gaps {ev:+.2f}R)")
    print()
