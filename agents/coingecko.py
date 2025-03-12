import requests
from agent_tooling import tool

@tool
def current_crypto_price(token: str) -> str:
    """A tool that handles requests for the current price of a cryptocurrency.

    Args:
        token: The name of the cryptocurrency.
    
    Returns:
        The current price of the cryptocurrency in USD, or an error message if not found.
    """

    # Define the API endpoint
    url = 'https://api.coingecko.com/api/v3/coins/list'
    
    # Fetch the list of all cryptocurrencies
    response = requests.get(url)
    
    if response.status_code == 200:
        coins = response.json()
        # Find the coin with the specified name
        coin = next((coin for coin in coins if coin['name'].lower() == token.lower()), None)

        if coin:
            coin_id = coin['id']

            # Define the API endpoint and parameters for fetching the price
            price_url = 'https://api.coingecko.com/api/v3/simple/price'
            params = {'ids': coin_id, 'vs_currencies': 'usd'}

            # Fetch the current price
            price_response = requests.get(price_url, params=params)
            
            if price_response.status_code == 200:
                price_data = price_response.json()
                if coin_id in price_data and 'usd' in price_data[coin_id]:
                    return f"Agent CoinGecko 🦎: The current price of {token} is ${price_data[coin_id]['usd']} USD."
                else:
                    return f"Agent CoinGecko 🦎: Price data for {token} is unavailable."
            else:
                return f"Agent CoinGecko 🦎: Failed to retrieve price data for {token}."
        else:
            return f"Agent CoinGecko 🦎: {token} not found in the coin list."
    else:
        return "Agent CoinGecko 🦎: Failed to retrieve the list of cryptocurrencies from CoinGecko API."