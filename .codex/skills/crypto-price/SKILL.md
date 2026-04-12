---
name: crypto-price
description: Get cryptocurrency prices via CoinGecko
---

# Crypto Price Tool

Get current USD price of a cryptocurrency via CoinGecko API.

## Usage

```bash
/usr/src/app/tools/stateless/crypto/venv/bin/python /usr/src/app/tools/stateless/crypto/crypto.py \
  --coin "<coin_id>"
```

## Arguments

- `--coin` (required): CoinGecko coin ID (e.g. "bitcoin", "ethereum", "solana")

## Output

JSON object with:
- `coin`: The coin ID queried
- `price_usd`: Current USD price
