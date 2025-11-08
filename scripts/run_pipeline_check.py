import argparse
from datetime import datetime, timedelta

from src.tools.api import get_market_data, get_price_history


def run(symbol: str) -> None:
    end_date = datetime.now() - timedelta(days=1)
    start_date = end_date - timedelta(days=365)

    price_df = get_price_history(
        symbol,
        start_date=start_date.strftime("%Y-%m-%d"),
        end_date=end_date.strftime("%Y-%m-%d"),
    )
    print(f"[{symbol}] price history rows: {len(price_df)}")
    if not price_df.empty:
        print(f"First date: {price_df['date'].iloc[0]}, last date: {price_df['date'].iloc[-1]}")

    market_snapshot = get_market_data(symbol)
    print(f"[{symbol}] market snapshot: {market_snapshot}")


def main():
    parser = argparse.ArgumentParser(description="Run BaoStock + snapshot pipeline sanity check.")
    parser.add_argument("--ticker", type=str, default="002475")
    args = parser.parse_args()
    run(args.ticker)


if __name__ == "__main__":
    main()
