import requests
from agent_tooling import tool
from agents.secret_manager import get_credential


@tool(tags=["money"])
def get_crypto_price(crypto_name: str) -> str:
    """Get the current USD price of a cryptocurrency via CoinGecko.

    Args:
        crypto_name: CoinGecko coin ID (e.g. "bitcoin", "ethereum", "solana")

    Returns:
        JSON string with price data or error message.
    """
    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {"ids": crypto_name.lower(), "vs_currencies": "usd"}
    headers = {}
    api_key = get_credential("COINGECKO_API_KEY")
    if api_key:
        headers["x-cg-pro-api-key"] = api_key

    response = requests.get(url, params=params, headers=headers, timeout=10)
    if response.status_code != 200:
        return f"CoinGecko API error: HTTP {response.status_code}"

    data = response.json()
    key = crypto_name.lower()
    if key in data:
        return f"{crypto_name}: ${data[key]['usd']} USD"
    return f"Could not find price for '{crypto_name}'. Use the CoinGecko coin ID (e.g. bitcoin, ethereum)."
