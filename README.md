# PartsPicker — PC Parts Price Tracker

Local morning price tracker for GPUs, RAM, SSDs, and custom searches.

## Run locally

```bat
pip install -r requirements.txt
playwright install chrome
start_server.bat
```

Open http://localhost:5001

- **Scan again** in the report (or `run_now.bat`) to scrape
- **Set ZIP** in the footer for Facebook Marketplace (100 mi radius)
- Click **+ Min price** on a card to ignore damaged/parts junk listings

## Retailers

Newegg · Best Buy · Micro Center · Amazon · eBay · Facebook Marketplace

eBay and Facebook open a brief Chrome window (they block headless bots).

## Config

Edit `config.json` or use the report UI:

- `settings.facebook_zipcode` — e.g. `"15213"`
- `settings.facebook_radius_miles` — default `100`
- per-item `min_price` / `target_price`
