# PartsPicker — PC Parts Price Tracker

Local morning price tracker for GPUs, RAM, SSDs, and custom searches.

## Phone link (GitHub Pages)

**https://jassmithx7-dev.github.io/PartsPicker/**

After a scrape, publish with `publish_report.bat` (or use `run_now.bat`, which publishes automatically).
Pages can take 1–2 minutes to update. View-only on phone (add/scan still need your PC).

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
- `publish_report.bat` — push latest report for phone
- `morning_run.bat` — scrape + publish (good for Task Scheduler)

## Retailers

Newegg · Amazon · eBay · Facebook Marketplace

eBay and Facebook open a brief minimized Chrome window (they block headless bots).

## Config

Edit `config.json` or use the report UI:

- `settings.facebook_zipcode` — e.g. `"15213"`
- `settings.facebook_radius_miles` — default `100`
- per-item `min_price` / `target_price`
