# AWS Backup Recovery Point Reconciliation – Project Summary

## Problem Statement
100+ application accounts each back up resources to local AWS Backup vaults.
All of those vaults replicate to a **single central account** via cross-account copy.
The goal is to verify that every recovery point in each app account (with retention
> 35 days) has a matching copy in the central account — and produce actionable reports
for any gaps.

---

## Overall Architecture

```
App Accounts (100+)                     Central Account
┌──────────────────────┐               ┌──────────────────────────────────────┐
│  Region A            │               │  Central Vaults (8 regions × 6 svc) │
│  ├─ RDS vault        │──────────────▶│  RDS / EBS / EC2 / EFS / FSx / S3   │
│  ├─ EBS vault        │               │                                      │
│  └─ ...              │               │  SSM Automation (per region)         │
│                      │               │  └─ Writes JSON index to S3          │
│  Region B (maybe)    │               │                                      │
│  └─ EBS vault        │               │  S3 bucket (ap-southeast-1)          │
└──────────────────────┘               │  central-index/<region>/<Type>.json  │
                                       └──────────────────────────────────────┘

Phase 2 reconciliation script
  reads S3 index (no central account API calls)
  loops each app account → compares → produces CSV reports
```

### Key Design Principles
- **Central vault scanned once** — SSM runbook writes to S3; Phase 2 reads S3, never re-queries the central Backup API
- **S3 versioning enabled** — daily runs overwrite the same keys; S3 retains prior versions for rollback/audit (lifecycle: expire non-current versions after 30 days)
- **Per-region SSM runbooks** — one document definition deployed to each of 8 regions; fault isolation; re-run one region independently
- **Vault discovery is dynamic** — `list_backup_vaults()` called at runtime; no hardcoded vault names in either phase
- **App accounts are heterogeneous** — each may have 1–3 active regions and 4–6 service vaults; Phase 2 handles this dynamically
- **Retention filter** — recovery points with retention ≤ 35 days are excluded from both phases

---

## Phases

### Phase 1 – Central Vault Index Collector  ✅ COMPLETE

**What it does:**
Runs from inside the central account. Discovers all Backup vaults in the executing
region, scans recovery points (retention > 35 days), extracts the source account ID
from each recovery point's `SourceBackupVaultArn`, and writes a structured JSON index
to S3.

**Trigger:** EventBridge Scheduler → SSM Automation, daily at 02:00 UTC, per region (8 rules).

**S3 output layout:**
```
s3://<CENTRAL_INDEX_BUCKET>/
  central-index/
    us-east-1/
      RDS.json
      EBS.json
      EC2.json
      EFS.json
      FSx.json
      S3.json
    us-west-2/
      RDS.json
      ...
    (sparse — only resource types that actually exist in that region)
```

**JSON file structure (by_source_account keyed for O(1) lookup in Phase 2):**
```json
{
  "collected_at": "2026-06-15T02:00:00Z",
  "region": "us-east-1",
  "resource_type": "RDS",
  "total_records": 142,
  "source_accounts": ["222222222222", "333333333333"],
  "by_source_account": {
    "222222222222": [
      {
        "rp_arn": "arn:aws:backup:us-east-1:111111111111:recovery-point:abc",
        "resource_arn": "arn:aws:rds:us-east-1:222222222222:db:mydb",
        "creation_date": "2026-06-14T02:00:00Z",
        "completion_date": "2026-06-14T02:45:00Z",
        "retention_days": 90,
        "delete_at": "2026-09-12T02:00:00Z",
        "status": "COMPLETED",
        "vault_name": "central-rds-vault",
        "backup_size_bytes": 10737418240
      }
    ]
  }
}
```

**Delivered files:**
| File | Purpose |
|------|---------|
| `phase1_collect_handler.py` | Python logic — standalone copy for local testing |
| `ssm_document.yaml` | SSM Automation document (deploy to each of 8 regions) |
| `iam_execution_role_policy.json` | IAM permissions policy for the SSM execution role |
| `iam_trust_policy.json` | IAM trust policy — allows ssm.amazonaws.com to assume the role |

**Before deploying Phase 1:**
- Replace `<CENTRAL_INDEX_BUCKET_NAME>` in `iam_execution_role_policy.json`
- Replace `<CENTRAL_ACCOUNT_ID>` in `iam_trust_policy.json`
- Update the 8-region list in `iam_execution_role_policy.json` to match your actual active regions
- Enable S3 versioning on the central index bucket
- Add S3 Lifecycle rule: expire non-current versions after 30 days
- Deploy `ssm_document.yaml` to each of the 8 regions via AWS CLI or Console
- Create 8 EventBridge Scheduler rules (one per region) targeting the SSM Automation

**IAM role name (suggested):** `BackupCentralIndexCollectRole`

---

### Phase 2 – App Account Reconciliation  ⏳ TO BE DESIGNED

**What it does:**
Reads the S3 index built by Phase 1 (no central account Backup API calls). For each
app account, assumes a cross-account role, auto-discovers vaults, scans recovery points
(retention > 35 days), and compares against the central index. Produces two CSV reports.

**Trigger:** On-demand (manual run or scheduled separately from Phase 1).

**How it reads the central index:**
- Loads S3 files lazily — only fetches `central-index/<region>/<ResourceType>.json`
  when the first app account recovery point of that region+type is encountered
- In-memory cache: at most 48 S3 reads across the entire run regardless of app account count

**Per-app-account flow:**
```
For each app_account_id in app_accounts.txt:
  STS AssumeRole → arn:aws:iam::<account>:role/<ROLE_NAME>
  For each region in REGIONS:
    list_backup_vaults()             ← auto-discover; skip region if empty
    For each vault:
      list_recovery_points (retention > 35 days)
      For each recovery point:
        resource_type → load central_cache[(region, resource_type)]
        look up central_cache[source_account_id]
        find nearest creation_date match (tolerance: 24 h)
        → MATCHED  or  MISSING_IN_CENTRAL (NO_COPY / DATE_MISMATCH)
  If role assumption fails → log, mark SKIPPED, continue
```

**Cross-account IAM role (to be created in every app account):**
- Role name must be consistent across all app accounts (e.g., `BackupReconciliationRole`)
- Trust: allows the central account (or a dedicated tooling account) to assume it
- Permissions needed: `backup:ListBackupVaults`, `backup:ListRecoveryPointsByBackupVault`

**Output reports:**
```
matched_report.csv
  app_account_id | region | resource_type | resource_arn
  app_rp_arn     | app_creation_date | app_retention_days | app_delete_at
  central_rp_arn | central_creation_date | central_retention_days | central_delete_at
  date_diff_hours

missing_in_central_report.csv
  app_account_id | region | resource_type | resource_arn
  app_rp_arn     | app_creation_date | app_retention_days | app_delete_at
  reason         (NO_COPY_IN_CENTRAL | DATE_MISMATCH)
  nearest_central_rp_arn    ← DATE_MISMATCH rows only
  nearest_central_date      ← DATE_MISMATCH rows only
  date_diff_hours           ← DATE_MISMATCH rows only
```

**Console summary printed at end:**
```
Central index as of  : <collected_at from S3>
App accounts         : 102 processed / 2 skipped (role error)
Recovery points      : N total scanned
  MATCHED            : n1
  MISSING_IN_CENTRAL : n2  (NO_COPY: n3 | DATE_MISMATCH: n4)
Reports              : matched_report.csv, missing_in_central_report.csv
```

**Inputs needed to design Phase 2:**
- Confirm app account list source: `app_accounts.txt` (flat file, one ID per line)
- Confirm cross-account role name to assume in app accounts
- Confirm whether Phase 2 runs from the central account, a tooling account, or locally
- Confirm the 8 active region list

**Files to be delivered for Phase 2:**
| File | Purpose |
|------|---------|
| `phase2_reconcile.py` | Main reconciliation script |
| `app_accounts.txt` | Sample/template — one account ID per line |
| `iam_app_account_role_policy.json` | IAM policy to deploy in each app account |
| `iam_app_account_trust_policy.json` | Trust policy for the app account role |

---

### Phase 3 – Future Enhancements  💡 NOT STARTED

Ideas discussed but not committed:
- **SNS / email notifications** when `missing_in_central_report.csv` is non-empty
- **EventBridge + Lambda** to trigger Phase 2 automatically after Phase 1 completes
- **S3 Select or Athena** to query the central index directly for ad-hoc lookups
- **CloudWatch Dashboard** showing matched vs missing counts over time
- **Automated tagging** of resources with missing central backups for visibility

---

## Configuration Reference

| Parameter | Phase | Value / Placeholder |
|-----------|-------|---------------------|
| Central account ID | 1 & 2 | `<CENTRAL_ACCOUNT_ID>` |
| Central index S3 bucket | 1 & 2 | `<CENTRAL_INDEX_BUCKET_NAME>` (ap-southeast-1) |
| S3 prefix | 1 & 2 | `central-index` |
| Retention threshold (days) | 1 & 2 | `35` (strictly greater than) |
| Date-match tolerance (hours) | 2 | `24` |
| SSM document name | 1 | `BackupCentralIndexCollect` |
| SSM execution role | 1 | `BackupCentralIndexCollectRole` |
| App account reconciliation role | 2 | `BackupReconciliationRole` (TBD) |
| Active regions | 1 & 2 | 8 regions (update list to match your environment) |
| S3 versioning | 1 | Enabled; non-current versions expire after 30 days |
| EventBridge schedule | 1 | `cron(0 2 * * ? *)` — daily 02:00 UTC, per region |

---

## File Index (this repository)

```
backup_recovery_point_reconcile/
│
├── PROJECT_SUMMARY.md               ← this file
│
├── Phase 1 (COMPLETE)
│   ├── phase1_collect_handler.py    ← Python handler (standalone + SSM inline copy)
│   ├── ssm_document.yaml            ← SSM Automation document (deploy to 8 regions)
│   ├── iam_execution_role_policy.json
│   └── iam_trust_policy.json
│
└── Phase 2 (TO BE BUILT)
    ├── phase2_reconcile.py          ← not yet created
    ├── app_accounts.txt             ← not yet created
    ├── iam_app_account_role_policy.json   ← not yet created
    └── iam_app_account_trust_policy.json  ← not yet created
```
