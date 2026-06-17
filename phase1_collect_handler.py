"""
Phase 1 - Central Backup Vault Index Collector

Runs inside an SSM Automation aws:executeScript step (Python 3.11).
Entry point: script_handler(events, context)

events keys (passed from SSM InputPayload):
    Region                  - target region  (injected via {{global:REGION}})
    S3Bucket                - bucket name in ap-southeast-1
    S3Prefix                - key prefix (default: "central-index")
    RetentionThresholdDays  - integer, default 35

For each active vault in the region the script:
  1. Auto-discovers all backup vaults (list_backup_vaults)
  2. Scans every recovery point, keeping only those with retention > threshold
  3. Extracts the source account ID from SourceBackupVaultArn
  4. Groups records by ResourceType and source account
  5. Writes one S3 JSON file per ResourceType:
       s3://<bucket>/<prefix>/<region>/<ResourceType>.json

S3 versioning is expected to be enabled on the bucket - each daily run
overwrites the same key and S3 retains previous versions automatically.
"""

import boto3
import json
from datetime import datetime, timezone
from typing import Optional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _derive_retention_days(rp: dict) -> Optional[int]:
    """
    Return retention in days from a raw recovery-point dict.
    Priority:
      1. Lifecycle.DeleteAfterDays  (explicitly configured at backup plan level)
      2. CalculatedLifecycle.DeleteAt - CreationDate  (AWS-computed)
      3. None  (unknown - caller treats as "include")
    """
    lc = rp.get("Lifecycle") or {}
    explicit = lc.get("DeleteAfterDays")
    if explicit:
        return int(explicit)

    calc = rp.get("CalculatedLifecycle") or {}
    delete_at: Optional[datetime] = calc.get("DeleteAt")
    created_at: Optional[datetime] = rp.get("CreationDate")
    if delete_at and created_at:
        return max(0, (delete_at - created_at).days)

    return None


def _iso(dt) -> str:
    """Safely convert a datetime (or None) to an ISO-8601 string."""
    if dt is None:
        return ""
    if isinstance(dt, datetime):
        return dt.isoformat()
    return str(dt)


def _source_account_from_arn(arn: str) -> Optional[str]:
    """Extract account ID (5th segment) from an ARN string."""
    if not arn:
        return None
    parts = arn.split(":")
    account = parts[4] if len(parts) > 4 else None
    return account if account else None


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

def script_handler(events: dict, context) -> dict:
    """
    SSM Automation entry point.
    Returns a summary dict that SSM captures as step output.
    """
    region              = events["Region"]
    s3_bucket           = events["S3Bucket"]
    s3_prefix           = events.get("S3Prefix", "central-index")
    retention_threshold = int(events.get("RetentionThresholdDays", 35))

    backup = boto3.client("backup", region_name=region)
    s3     = boto3.client("s3",     region_name="ap-southeast-1")

    # -- 1. Discover all vaults in this region --------------------------------
    vaults = []
    try:
        paginator = backup.get_paginator("list_backup_vaults")
        for page in paginator.paginate():
            vaults.extend(page.get("BackupVaultList", []))
    except Exception as exc:
        raise RuntimeError(f"[{region}] Cannot list backup vaults: {exc}") from exc

    print(f"[{region}] Discovered {len(vaults)} vault(s)")

    # -- 2. Scan each vault ---------------------------------------------------
    # Structure: { resource_type: { source_account_id: [ record, ... ] } }
    collected: dict = {}
    total_kept        = 0
    skipped_retention = 0
    skipped_no_source = 0

    for vault in vaults:
        vault_name = vault["BackupVaultName"]
        vault_rps  = 0
        try:
            paginator = backup.get_paginator("list_recovery_points_by_backup_vault")
            for page in paginator.paginate(BackupVaultName=vault_name):
                for rp in page.get("RecoveryPoints", []):

                    # Derive retention and apply threshold filter
                    retention_days = _derive_retention_days(rp)
                    if retention_days is not None and retention_days <= retention_threshold:
                        skipped_retention += 1
                        continue

                    # Source account must be identifiable
                    source_account_id = _source_account_from_arn(
                        rp.get("SourceBackupVaultArn", "")
                    )
                    if not source_account_id:
                        skipped_no_source += 1
                        continue

                    resource_type = rp.get("ResourceType", "UNKNOWN")
                    calc          = rp.get("CalculatedLifecycle") or {}

                    record = {
                        "rp_arn":            rp["RecoveryPointArn"],
                        "resource_arn":      rp.get("ResourceArn", ""),
                        "creation_date":     _iso(rp.get("CreationDate")),
                        "completion_date":   _iso(rp.get("CompletionDate")),
                        "retention_days":    retention_days,
                        "delete_at":         _iso(calc.get("DeleteAt")),
                        "status":            rp.get("Status", ""),
                        "vault_name":        vault_name,
                        "backup_size_bytes": rp.get("BackupSizeInBytes"),
                    }

                    # Parentheses allow multi-line method chaining (implicit line joining) without backslashes
                    (
                        collected
                        .setdefault(resource_type, {})
                        .setdefault(source_account_id, [])
                        .append(record)
                    )
                    total_kept += 1
                    vault_rps  += 1

        except Exception as exc:
            # One bad vault must not abort the whole region run
            print(f"[{region}] WARNING: error scanning vault '{vault_name}': {exc}")

        print(f"[{region}]   vault '{vault_name}' -> {vault_rps} record(s) kept")

    print(
        f"[{region}] Scan complete. "
        f"kept={total_kept} | skipped_retention={skipped_retention} | "
        f"skipped_no_source={skipped_no_source}"
    )

    # -- 3. Write one S3 file per ResourceType --------------------------------
    files_written    = []
    collected_at_str = datetime.now(timezone.utc).isoformat()

    for resource_type, by_account in collected.items():
        s3_key  = f"{s3_prefix}/{region}/{resource_type}.json"
        payload = {
            "collected_at":      collected_at_str,
            "region":            region,
            "resource_type":     resource_type,
            "total_records":     sum(len(v) for v in by_account.values()),
            "source_accounts":   sorted(by_account.keys()),
            "by_source_account": by_account,
        }
        try:
            s3.put_object(
                Bucket=s3_bucket,
                Key=s3_key,
                Body=json.dumps(payload, default=str),
                ContentType="application/json",
            )
            files_written.append(s3_key)
            print(f"[{region}] Written -> s3://{s3_bucket}/{s3_key}  "
                  f"({payload['total_records']} records, "
                  f"{len(by_account)} source accounts)")
        except Exception as exc:
            raise RuntimeError(
                f"[{region}] S3 write failed for {s3_key}: {exc}"
            ) from exc

    print(f"[{region}] Done. {len(files_written)} file(s) written to S3.")

    return {
        "region":                  region,
        "vaults_scanned":          len(vaults),
        "total_records_collected": total_kept,
        "files_written":           files_written,
        "resource_types_found":    list(collected.keys()),
        "skipped_retention":       skipped_retention,
        "skipped_no_source":       skipped_no_source,
    }
