"""Which kind of 'aggressive' actually helps? Compare policies under three edge scenarios.
Same assumptions as policy_sim.py. Ladders map drawdown-from-peak -> risk fraction; 0 = halted for the year."""
import numpy as np

rng = np.random.default_rng(11)
N = 20000
scen = {"A real edge": (0.45, 1.8), "B weak edge": (0.40, 1.6), "C no edge": (0.38, 1.6)}


def ladder_std(dd):
    return np.select([dd < .05, dd < .08, dd < .12, dd < .15], [.015, .01125, .0075, .00375], 0.0)


def ladder_agg_tight(dd):  # as first drafted: 2.5% only while DD<6%, 2.0% to 9%, then standard ladder
    return np.select([dd < .06, dd < .09, dd < .12, dd < .15], [.025, .020, .0075, .00375], 0.0)


def ladder_agg_wide(dd):   # wider tolerance, halt at 25%
    return np.select([dd < .10, dd < .15, dd < .20, dd < .25], [.025, .020, .010, .005], 0.0)


def ladder_agg_mid(dd):    # 2.0% max, halt at 20%
    return np.select([dd < .08, dd < .12, dd < .16, dd < .20], [.020, .015, .0075, .00375], 0.0)


def run(p, b, ladder, T):
    out = np.where(rng.random((N, T)) < p, b, -1.0)
    eq = np.ones(N); peak = np.ones(N); halted = np.zeros(N, bool)
    for t in range(T):
        dd = 1 - eq / peak
        f = ladder(dd)
        halted |= f == 0
        f = np.where(halted, 0, f)
        eq *= 1 + f * out[:, t]
        peak = np.maximum(peak, eq)
    return eq, halted


policies = [("STANDARD 1.5%, halt 15%, 250 trades", ladder_std, 250),
            ("AGG-TIGHT 2.5%, halt 15%, 250 trades", ladder_agg_tight, 250),
            ("AGG-MID 2.0%, halt 20%, 250 trades", ladder_agg_mid, 250),
            ("AGG-WIDE 2.5%, halt 25%, 250 trades", ladder_agg_wide, 250),
            ("BREADTH: STANDARD ladder, 400 trades", ladder_std, 400)]

print(f"{'scenario':12} {'policy':40} {'median x':>9} {'5th pct':>8} {'P(halt)':>8} {'P(loss)':>8}")
for s, (p, b) in scen.items():
    for name, lad, T in policies:
        eq, h = run(p, b, lad, T)
        print(f"{s:12} {name:40} {np.median(eq):>9.2f} {np.percentile(eq,5):>8.2f} "
              f"{h.mean()*100:>7.1f}% {(eq<1).mean()*100:>7.1f}%")
    print()
