from google.cloud import bigquery
from config import settings
from agents.pipeline import _get_credentials

creds = _get_credentials()
client = bigquery.Client(project=settings.PROJECT_ID, location=settings.GCP_REGION, credentials=creds)

q = f"""
SELECT record_id, deviation_id, phase, prev_record_hash, record_hash, created_at
FROM `{settings.PROJECT_ID}.{settings.BQ_DATASET}.audit_ledger`
ORDER BY created_at ASC
"""
rows = list(client.query(q).result())
print("Total rows:", len(rows))

target_idx = None
for idx, r in enumerate(rows):
    if r["record_id"] == "REC-B092CD6AEA48":
        target_idx = idx
        break

print("Target index:", target_idx)
if target_idx is not None:
    start = max(0, target_idx - 3)
    end = min(len(rows), target_idx + 4)
    for i in range(start, end):
        r = rows[i]
        print(f"Row {i:4d}: ID={r['record_id']} | Phase={r['phase']:<8} | Prev={r['prev_record_hash'][:16]} | Hash={r['record_hash'][:16]} | Created={r['created_at']}")
