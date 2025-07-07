import requests
from agent_tooling import tool
import os


@tool(tags=["money"])
def get_crypto_price(crypto_name):
    """Use this agent to get the current price of a cryptocurrency."""
    url = 'https://api.coingecko.com/api/v3/simple/price'
    params = {
        'ids': crypto_name.lower(),
        'vs_currencies': 'usd'
    }
    headers = {
        'x-cg-pro-api-key': os.getenv('COINGECKO_API_KEY')
    }
    response = requests.get(url, params=params, headers=headers)

    if response.status_code == 200:
        data = response.json()
        if crypto_name.lower() in data:
            price = data[crypto_name.lower()]['usd']
            return f'CoinGecko 🦎: The price of {crypto_name.capitalize()} in USD is ${price}'
        else:
            return f'CoinGecko 🦎: Sorry, I could not find the price for {crypto_name}.'
    else:
        return 'CoinGecko 🦎: Failed to retrieve data from the API.'






def current_crypto_price(token: str, messages) -> str:
    """Choose this tool when the user asks for the current price of a cryptocurrency."""

    #yield "Still using the janky CoinGecko API tool...\n\n\n"

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