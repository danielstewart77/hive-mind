---
name: weather
description: Get weather for a location
---

# Weather Tool

Get current or forecasted weather for any location.

## Usage

```bash
/usr/src/app/tools/stateless/weather/venv/bin/python /usr/src/app/tools/stateless/weather/weather.py \
  --location "<city, state>" \
  --time-span "<today|tonight|this week|this weekend>"
```

## Arguments

- `--location` (optional): City and state/country (default: "missouri city, tx")
- `--time-span` (optional): "today" (default), "tonight", "this week", or "this weekend"

## Output

JSON object with temperature, conditions, and forecast data.
