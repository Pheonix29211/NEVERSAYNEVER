from .features import compute_features

def simulate_universe(universe, args):
    for row in universe:
        feats = compute_features(row)
        print("SIM", row["token"], feats)
