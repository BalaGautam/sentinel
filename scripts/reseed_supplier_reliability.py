"""Re-seed delivery_history with realistic on-time delivery spread (0.62 to 0.94) and compute supplier_reliability."""

from google.cloud import bigquery
from config import settings
from agents.pipeline import _get_credentials

creds = _get_credentials()
client = bigquery.Client(project=settings.PROJECT_ID, location=settings.GCP_REGION, credentials=creds)

# 1. Generate 200 delivery_history rows with realistic on-time distribution
rows = []
delays_map = {
    1: {1: 3, 4: 2, 7: 4, 10: 2, 13: 3, 15: 2},
    2: {5: 1},
    3: {3: 2, 11: 1},
    4: {7: 1},
    5: {2: 2, 8: 1, 14: 2},
    6: {9: 1},
    7: {1: 3, 3: 2, 6: 4, 9: 2, 12: 3, 15: 1},
    8: {4: 1, 12: 2},
    9: {2: 2, 6: 3, 10: 1, 14: 2},
    10: {8: 1},
    11: {3: 2, 7: 3, 11: 1, 15: 2},
    12: {5: 2, 13: 1},
}

import datetime

today = datetime.date.today()

for idx in range(1, 201):
    sup_num = (idx % 12) + 1
    sup_id = f"SUP-{sup_num:02d}"
    seq = (idx - 1) // 12
    
    delay_days = delays_map.get(sup_num, {}).get(seq, 0)
    promised_date = today - datetime.timedelta(days=(idx % 90))
    actual_date = promised_date + datetime.timedelta(days=delay_days)
    
    quoted = 50.0 + (idx % 15) * 10
    drift_pct = (sup_num * 0.25) / 100.0 if (seq % 3 == 0) else 0.0
    invoiced = round(quoted * (1.0 + drift_pct), 2)
    
    qty_ord = 100 + (idx % 5) * 50
    qty_rec = qty_ord - (20 if (idx % 12 == 0) else 0)
    
    rows.append({
        "delivery_id": f"DEL-{idx:04d}",
        "supplier_id": sup_id,
        "po_id": f"PO-{10000 + idx}",
        "sku_id": f"SKU-{(idx % 20) + 1:03d}",
        "promised_date": promised_date.isoformat(),
        "actual_delivery_date": actual_date.isoformat(),
        "quoted_price_usd": str(quoted),
        "invoiced_price_usd": str(invoiced),
        "quantity_ordered": qty_ord,
        "quantity_received": qty_rec,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    })

# Truncate and insert into delivery_history
dh_table = f"{settings.PROJECT_ID}.{settings.BQ_DATASET}.delivery_history"
client.query(f"DELETE FROM `{dh_table}` WHERE 1=1").result()
errors = client.insert_rows_json(dh_table, rows)
if errors:
    raise RuntimeError(f"Error inserting into delivery_history: {errors}")
print(f"Seeded {len(rows)} rows into {dh_table}.")

# 2. Populate supplier_reliability by aggregating delivery_history
sr_sql = f"""
CREATE OR REPLACE TABLE `{settings.PROJECT_ID}.{settings.BQ_DATASET}.supplier_reliability` AS
SELECT
    s.supplier_id,
    ROUND(COALESCE(IEEE_DIVIDE(COUNTIF(d.actual_delivery_date <= d.promised_date), NULLIF(COUNT(d.delivery_id), 0)), 0.0), 4) AS on_time_rate_90d,
    ROUND(COALESCE(AVG(DATE_DIFF(d.actual_delivery_date, d.promised_date, DAY)), 0.0), 2) AS avg_lead_time_drift_days,
    ROUND(COALESCE(IEEE_DIVIDE(SUM(d.invoiced_price_usd - d.quoted_price_usd), NULLIF(SUM(d.quoted_price_usd), 0)), 0.0), 4) AS quote_variance_rate,
    COUNT(d.delivery_id) AS sample_size,
    'computed' AS provenance,
    CURRENT_TIMESTAMP() AS computed_at
FROM `{settings.PROJECT_ID}.{settings.BQ_DATASET}.supplier_master` s
LEFT JOIN `{settings.PROJECT_ID}.{settings.BQ_DATASET}.delivery_history` d
    ON s.supplier_id = d.supplier_id
    AND d.promised_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY)
GROUP BY s.supplier_id
"""

client.query(sr_sql).result()
print("Populated supplier_reliability successfully.")
