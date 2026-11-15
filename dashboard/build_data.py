"""Aggregate the VoltRelay CSVs into a compact JSON file for the web dashboard.

Follows POWER_BI_BUILD_SPEC.md: no rows dropped from source; firmware v3.2.0
timestamp fix applied; test stations are flagged and excluded from business
KPIs (they are reported separately in the data-quality panel).

Run from the hackathon folder:  python dashboard/build_data.py
"""
import json
import os
from collections import defaultdict

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "dashboard", "data.js")
P = lambda f: os.path.join(ROOT, f)

CITY_MAP = {
    "bengaluru": "Bengaluru", "bangalore": "Bengaluru", "blr": "Bengaluru",
    "delhi ncr": "Delhi NCR", "delhi": "Delhi NCR", "new delhi": "Delhi NCR", "gurgaon": "Delhi NCR",
    "gurugram": "Delhi NCR", "noida": "Delhi NCR", "del": "Delhi NCR",
    "hyderabad": "Hyderabad", "hyd": "Hyderabad",
    "pune": "Pune", "pun": "Pune",
    "mumbai": "Mumbai", "bombay": "Mumbai", "mum": "Mumbai",
    "jaipur": "Jaipur", "jai": "Jaipur",
}

# ---------------------------------------------------------------- dimensions
stations = pd.read_csv(P("stations.csv"))
stations["is_test"] = stations.station_id.str.startswith("STN-TST")
st_city = stations.set_index("station_id").city.to_dict()
st_tariff = stations.set_index("station_id").grid_tariff_inr_kwh.to_dict()
st_test = stations.set_index("station_id").is_test.to_dict()

riders = pd.read_csv(P("riders.csv"))
riders["city_std"] = riders.home_city.astype(str).str.strip().str.lower().map(CITY_MAP).fillna(riders.home_city)
rider_partner = riders.set_index("rider_id").partner_id.to_dict()
partners = pd.read_csv(P("fleet_partners.csv"))
pname = partners.set_index("partner_id").partner_name.to_dict()
pseg = partners.set_index("partner_id").partner_segment.to_dict()

# ---------------------------------------------------------------- swap events
cols = ["event_id", "rider_id", "station_id", "event_ts", "event_type", "queue_wait_sec",
        "km_since_last_swap", "tariff_code", "discount_inr", "amount_charged_inr",
        "energy_to_recharge_kwh", "station_firmware", "sync_mode"]

monthly = []        # per chunk groupby frames, concatenated later
by_type = []
by_hour = []
by_station = []
by_partner = []
active = defaultdict(set)     # (month, city) -> riders with a completed swap
active_all = defaultdict(set)
dq = defaultdict(int)
event_ids = []

for i, ch in enumerate(pd.read_csv(P("swap_events.csv"), usecols=cols, chunksize=750_000)):
    ts = pd.to_datetime(ch.event_ts)
    d = ts.dt.normalize()
    shift = (ch.station_firmware == "v3.2.0") & (d >= "2025-03-10") & (d <= "2025-04-14")
    dq["tz_shifted"] += int(shift.sum())
    ts = ts.where(~shift, ts + pd.Timedelta(hours=5, minutes=30))
    ch["month"] = ts.dt.strftime("%Y-%m")
    ch["hour"] = ts.dt.hour
    ch["dow"] = ts.dt.dayofweek
    ch["city"] = ch.station_id.map(st_city).fillna("Unknown")
    ch["is_test"] = ch.station_id.str.startswith("STN-TST")
    ch["completed"] = (ch.event_type == "swap_completed").astype(int)
    ch["failed"] = 1 - ch["completed"]
    ch["energy_cost"] = ch.energy_to_recharge_kwh * ch.station_id.map(st_tariff)
    ch["qw_n"] = ch.queue_wait_sec.notna().astype(int)
    ch["partner"] = ch.rider_id.map(rider_partner).fillna("RETAIL")

    km = ch.km_since_last_swap
    dq["rows"] += len(ch)
    dq["test_rows"] += int(ch.is_test.sum())
    dq["km_missing"] += int(km.isna().sum())
    dq["km_outlier"] += int(((km < 0) | (km > 500)).sum())
    dq["offline_batch"] += int((ch.sync_mode == "offline_batch").sum())
    dq["amount_missing"] += int(ch.amount_charged_inr.isna().sum())
    event_ids.append(ch.event_id.str[3:].astype(np.int64).values)

    b = ch[~ch.is_test]
    agg = dict(attempts=("event_id", "size"), completed=("completed", "sum"), failed=("failed", "sum"),
               revenue=("amount_charged_inr", "sum"), energy_cost=("energy_cost", "sum"),
               energy_kwh=("energy_to_recharge_kwh", "sum"), discount=("discount_inr", "sum"),
               qw_sum=("queue_wait_sec", "sum"), qw_n=("qw_n", "sum"))
    monthly.append(b.groupby(["month", "city"]).agg(**agg))
    by_type.append(b.groupby(["city", "event_type"]).size().rename("n"))
    by_hour.append(b.groupby(["city", "dow", "hour"]).agg(attempts=("event_id", "size"), failed=("failed", "sum")))
    by_station.append(ch.groupby("station_id").agg(**agg))
    by_partner.append(b.groupby(["city", "partner"]).agg(attempts=("event_id", "size"), completed=("completed", "sum"),
                                                          revenue=("amount_charged_inr", "sum"), discount=("discount_inr", "sum")))
    c = b[b.completed == 1]
    for (m, cy), g in c.groupby(["month", "city"]).rider_id:
        u = set(g.unique())
        active[(m, cy)] |= u
        active_all[m] |= u
    print(f"chunk {i}: {dq['rows']:,} rows", flush=True)

ids = np.concatenate(event_ids)
dq["dup_event_ids"] = int(len(ids) - len(np.unique(ids)))

sumg = lambda frames, keys: pd.concat(frames).groupby(level=keys).sum()
monthly = sumg(monthly, [0, 1]).reset_index()
by_type = sumg(by_type, [0, 1]).reset_index()
by_hour = sumg(by_hour, [0, 1, 2]).reset_index()
by_station = sumg(by_station, [0]).reset_index()
by_partner = sumg(by_partner, [0, 1]).reset_index()

# ---------------------------------------------------------------- hourly status
hs_cols = ["station_id", "hour_start", "charged_2w_min", "outage_minutes", "telemetry_status"]
hs_station, hs_month = [], []
for ch in pd.read_csv(P("station_hourly_status.csv"), usecols=hs_cols, chunksize=1_000_000):
    ch["month"] = ch.hour_start.str[:7]
    ch["city"] = ch.station_id.map(st_city).fillna("Unknown")
    ch["obs"] = ch.charged_2w_min.notna().astype(int)
    ch["stockout"] = (ch.charged_2w_min == 0).astype(int)
    ch["tel_missing"] = (ch.telemetry_status == "missing").astype(int)
    ch["tel_partial"] = (ch.telemetry_status == "partial").astype(int)
    ch["hours"] = 1
    a = dict(hours=("hours", "sum"), obs=("obs", "sum"), stockout=("stockout", "sum"),
             outage=("outage_minutes", "sum"), tel_missing=("tel_missing", "sum"), tel_partial=("tel_partial", "sum"))
    hs_station.append(ch.groupby("station_id").agg(**a))
    hs_month.append(ch[~ch.station_id.str.startswith("STN-TST")].groupby(["month", "city"]).agg(**a))
hs_station = sumg(hs_station, [0]).reset_index()
hs_month = sumg(hs_month, [0, 1]).reset_index()

# ---------------------------------------------------------------- tickets
tk = pd.read_csv(P("support_tickets.csv"))
tk["month"] = tk.created_ts.str[:7]
tk["city"] = tk.station_id.map(st_city)
tk["city"] = tk.city.fillna(tk.rider_id.map(riders.set_index("rider_id").city_std)).fillna("Unknown")
tk["unresolved"] = (tk.resolution_status == "unresolved").astype(int)
tk["has_csat"] = tk.csat_score.notna().astype(int)
tk["csat_sum"] = tk.csat_score.fillna(0)
tk["res_h"] = tk.resolution_hours.fillna(0)
tk["res_n"] = tk.resolution_hours.notna().astype(int)
tk_cat = tk.groupby(["city", "category"]).agg(n=("ticket_id", "size"), unresolved=("unresolved", "sum"),
                                               csat_sum=("csat_sum", "sum"), csat_n=("has_csat", "sum"),
                                               res_h=("res_h", "sum"), res_n=("res_n", "sum")).reset_index()
tk_month = tk.groupby(["month", "city"]).agg(n=("ticket_id", "size"), unresolved=("unresolved", "sum"),
                                             csat_sum=("csat_sum", "sum"), csat_n=("has_csat", "sum")).reset_index()
tk_chan = tk.groupby(["city", "channel"]).size().rename("n").reset_index()
tk_station = tk.groupby("station_id").agg(tickets=("ticket_id", "size")).reset_index()

# ---------------------------------------------------------------- batteries
bt = pd.read_csv(P("batteries.csv"))
bt["age_days"] = (pd.Timestamp("2025-06-30") - pd.to_datetime(bt.commission_date)).dt.days
bt["soh_loss"] = bt.initial_soh_pct - bt.current_soh_pct
bt["retired"] = bt.retired_date.notna()
bt["age_bucket"] = pd.cut(bt.age_days, [-1, 180, 365, 540, 730, 99999],
                          labels=["0-6m", "6-12m", "12-18m", "18-24m", "24m+"]).astype(str)
bat_sup = bt.groupby("supplier").agg(packs=("battery_id", "size"), soh=("current_soh_pct", "mean"),
                                     loss=("soh_loss", "mean"), retired=("retired", "sum"),
                                     below80=("current_soh_pct", lambda s: int((s < 80).sum()))).reset_index()
bat_age = bt.groupby(["age_bucket", "supplier"]).current_soh_pct.mean().round(2).reset_index()
bat_hist = np.histogram(bt.current_soh_pct.dropna(), bins=np.arange(60, 102, 2))
bat_ret = bt[bt.retired].retirement_reason.fillna("unspecified").value_counts().to_dict()

# ---------------------------------------------------------------- assemble
def rec(df):
    df = df.copy()
    for c in df.columns:
        if df[c].dtype.kind == "f":
            df[c] = df[c].round(2)
    return df.to_dict(orient="records")

st = stations.merge(by_station, on="station_id", how="left").merge(hs_station, on="station_id", how="left") \
             .merge(tk_station, on="station_id", how="left")
st = st[["station_id", "city", "zone", "location_type", "charger_generation", "latitude", "longitude",
         "is_test", "attempts", "completed", "failed", "revenue", "energy_cost", "qw_sum", "qw_n",
         "hours", "obs", "stockout", "outage", "tel_missing", "tickets", "monthly_rent_inr",
         "monthly_maintenance_inr", "decommissioned_date", "competitor_within_1_5km_since"]].fillna(
    {"attempts": 0, "completed": 0, "failed": 0, "revenue": 0, "energy_cost": 0, "tickets": 0})
st["decommissioned_date"] = st.decommissioned_date.fillna("")
st["competitor_within_1_5km_since"] = st.competitor_within_1_5km_since.fillna("")

by_partner["name"] = by_partner.partner.map(pname).fillna("Retail (no partner)")
by_partner["segment"] = by_partner.partner.map(pseg).fillna("retail")

active_rows = [{"month": m, "city": c, "active": len(s)} for (m, c), s in active.items()]
active_rows += [{"month": m, "city": "__ALL__", "active": len(s)} for m, s in active_all.items()]

dq.update({
    "riders": int(len(riders)),
    "riders_city_variants": int((riders.home_city != riders.city_std).sum()),
    "stations": int(len(stations)),
    "test_stations": int(stations.is_test.sum()),
    "tickets": int(len(tk)),
    "tickets_no_csat": int((tk.has_csat == 0).sum()),
    "tickets_duplicate": int((tk.resolution_status == "duplicate").sum()),
    "telemetry_hours": int(hs_station.hours.sum()),
    "telemetry_missing": int(hs_station.tel_missing.sum()),
    "telemetry_partial": int(hs_station.tel_partial.sum()),
    "batteries": int(len(bt)),
})

data = {
    "generated": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
    "monthly": rec(monthly), "byType": rec(by_type), "byHour": rec(by_hour),
    "stations": rec(st), "partners": rec(by_partner), "active": active_rows,
    "hsMonth": rec(hs_month), "tkCat": rec(tk_cat), "tkMonth": rec(tk_month), "tkChan": rec(tk_chan),
    "batSup": rec(bat_sup), "batAge": rec(bat_age),
    "batHist": {"bins": bat_hist[1][:-1].tolist(), "counts": bat_hist[0].tolist()},
    "batRetired": bat_ret,
    "partnerInfo": rec(partners.fillna("")),
    "dq": dict(dq),
}
with open(OUT, "w", encoding="utf-8") as f:
    f.write("window.VR_DATA = ")
    json.dump(data, f, separators=(",", ":"), default=lambda o: o.item() if hasattr(o, "item") else str(o))
    f.write(";\n")
print("wrote", OUT, os.path.getsize(OUT) // 1024, "KB")
