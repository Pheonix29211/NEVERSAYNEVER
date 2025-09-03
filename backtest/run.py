import argparse
from .loader import load_universe
from .sim import simulate_universe

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()
    uni = load_universe(args.days)
    simulate_universe(uni, args)

if __name__ == "__main__":
    main()
