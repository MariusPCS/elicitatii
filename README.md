# ANAF Property Auctions

A local viewer for the real-estate auctions on [elicitatii.anaf.ro](https://elicitatii.anaf.ro/licitatii/categorii/bunuri-imobile)
("bunuri imobile"). It shows every current auction on a map, as cards and in a sortable table, with filters
and a log of what changed between refreshes.

## Run it

Double-click `start.command` in Finder, or:

```bash
python3 server.py            # opens http://127.0.0.1:8765
python3 server.py --auto 6   # also refresh automatically every 6 hours
python3 server.py --no-browser --port 8765
```

It needs only Python 3, with no extra packages. The page must be opened through the server: opening
`index.html` directly can't load the data or run a refresh. The server listens on `127.0.0.1` only.

## What you can do

- **Map:** pins are placed from each auction's *Loc deținere* address. A pin is street-level when the
  street is found, otherwise at the village or town. A dashed pin is only at the county level.
- **Cards and table:** photo, starting price, valuation, price per m², discount, guarantee, minimum bid
  step, round, bids, encumbrances and a countdown to the end.
- **Filters:** search, property type, price, area, rooms, discount and end date. Also land zone
  (intravilan or extravilan), full ownership, no encumbrances, bids, auction round, location accuracy,
  bids below the starting price, county, favorites, and new or price-changed auctions.
- **Bids below starting price:** this is flagged only when the auction page itself says it is allowed,
  down to 25% of valuation, and only if there are at least two bidders.
- **Change log:** shows new and removed auctions, and changes to price, status, dates, discount or bids.
- **Export:** CSV of the filtered list.
- **Saved in your browser:** favorites and filters. Filters are also kept in the URL, so you can
  bookmark a search.

## Data

Everything lives in `data/`:

| File | Contents |
|---|---|
| `auctions.json` | Current list, merged with details and map positions |
| `changelog.json` | One entry per refresh, with the changes found |
| `details.json` | Cache of each auction's detail page: address, valuation, guarantee, bids… |
| `counties.json` | Auction → county |
| `geocache.json` | OpenStreetMap Nominatim lookups |

## Being polite to ANAF

- **Requests per refresh:** the listing is fetched with `limit=100`, so about 2 requests.
- **Detail pages:** each one is fetched once and cached. Pages only for new auctions are fetched later,
  plus at most 20 re-checks of bid counts older than 12 hours.
- **Spacing:** requests are spaced by a random 3–7 seconds and use a normal browser User-Agent.
- **Blocks:** a refresh stops on HTTP 403, 429 or 503 instead of retrying.
- **Refresh lock:** the Refresh button is locked for 10 minutes after each successful refresh.
- **Geocoding:** Nominatim is called at most once every 1.5 s, and its results are cached.

## Files

- `server.py`: local web server, refresh endpoint, and optional scheduled refreshes.
- `scraper.py`: listing and detail-page parsing (the Next.js flight payload), address parsing,
  geocoding and change detection.
- `index.html`: the whole page (Leaflet + MarkerCluster from cdnjs, OpenStreetMap tiles).
