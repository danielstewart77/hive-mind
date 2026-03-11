#!/usr/bin/env python3
"""Get cryptocurrency prices via CoinGecko API.

Standalone stateless tool. Dependencies: requests.
"""

import argparse
import json
import os
import sys

# Allow importing core.secrets when run from project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))


# Test mode mock data
_MOCK_PRICES = {
    "bitcoin": 67500.42,
    "ethereum": 3450.18,
    "solana": 142.55,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Get crypto price from CoinGecko")
    parser.add_argument("--coin", required=True, help="CoinGecko coin ID (e.g. bitcoin, ethereum)")
    parser.add_argument("--test-mode", action="store_true", help="Use mock data instead of real API")
    args = parser.parse_args()

    coin = args.coin.lower()

    if args.test_mode:
        if coin in _MOCK_PRICES:
            print(json.dumps({"coin": coin, "price_usd": _MOCK_PRICES[coin]}))
            return 0
        else:
            print(json.dumps({"error": f"Could not find price for '{coin}'."}))
            return 1

    try:
        import requests
        from core.secrets import get_credential

        url = "https://api.coingecko.com/api/v3/simple/price"
        params = {"ids": coin, "vs_currencies": "usd"}
        headers: dict[str, str] = {}
        api_key = get_credential("COINGECKO_API_KEY")
        if api_key:
            headers["x-cg-pro-api-key"] = api_key

        response = requests.get(url, params=params, headers=headers, timeout=10)
        if response.status_code != 200:
            print(json.dumps({"error": f"CoinGecko API error: HTTP {response.status_code}"}))
            return 1

        data = response.json()
        if coin in data:
            print(json.dumps({"coin": coin, "price_usd": data[coin]["usd"]}))
            return 0

        print(json.dumps({"error": f"Could not find price for '{coin}'. Use the CoinGecko coin ID."}))
        return 1

    except Exception as e:
        print(json.dumps({"error": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
