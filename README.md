# VoltRelay

VoltRelay is an operations analytics dashboard for an electric two-wheeler battery-swapping network.

The repository contains the complete runnable dashboard package in [`dashboard/`](./dashboard/), including:

- Executive KPIs, revenue, margin, demand, and failure analysis
- Station operations and city scorecards
- Rider and fleet-partner performance
- Customer support and CSAT reporting
- Battery health and supplier degradation analysis
- Transparent data-quality reporting
- A Python pipeline for generating the browser data bundle

## Run the dashboard

From the `dashboard` directory, start a local static server:

```powershell
cd dashboard
python -m http.server 8000
```

Open <http://localhost:8000> in a browser.

## Project documentation

See [`dashboard/README.md`](./dashboard/README.md) for the full feature overview, data model, regeneration instructions, metric definitions, and limitations.

## Data availability

The dashboard includes a generated `data.js` snapshot so it can be viewed immediately. Raw operational datasets are intentionally not committed to this repository because some source files are very large. To regenerate the snapshot, place the expected CSV inputs in the parent project directory and follow the instructions in [`dashboard/README.md`](./dashboard/README.md).
