"""Run history logging to Azure Blob Storage."""

import json
import logging
from datetime import datetime, timezone
from typing import List, Optional
from azure.storage.blob import ContainerClient
from azure.identity import DefaultAzureCredential

logger = logging.getLogger(__name__)

LOG_CONTAINER = "run-logs"


class RunLogger:
    """Writes a JSON log entry per function run to blob storage for user review."""

    def __init__(self, storage_account_name: str):
        account_url = f"https://{storage_account_name}.blob.core.windows.net"
        credential = DefaultAzureCredential()
        self.container_client = ContainerClient(account_url, LOG_CONTAINER, credential)

    def write(
        self,
        *,
        m365_version: Optional[int],
        total_routes: int,
        added: List[str],
        removed: List[str],
        drift_restored: List[str],
        add_succeeded: int,
        add_failed: int,
        remove_succeeded: int,
        remove_failed: int,
        result: str,
        error: Optional[str] = None
    ) -> None:
        now = datetime.now(timezone.utc)
        blob_name = now.strftime("%Y/%m/%d/%H-%M-%S") + ".json"

        new_from_m365 = [r for r in added if r not in set(drift_restored)]

        entry = {
            "timestamp": now.isoformat().replace("+00:00", "Z"),
            "result": result,
            "m365_version": m365_version,
            "total_routes": total_routes,
            "routes_added": added,
            "new_from_m365": new_from_m365,
            "drift_restored": drift_restored,
            "routes_removed": removed,
            "add_succeeded": add_succeeded,
            "add_failed": add_failed,
            "remove_succeeded": remove_succeeded,
            "remove_failed": remove_failed,
        }
        if error:
            entry["error"] = error

        try:
            self.container_client.upload_blob(
                name=blob_name,
                data=json.dumps(entry, indent=2),
                overwrite=True
            )
            logger.info(f"Run log written: {LOG_CONTAINER}/{blob_name}")
        except Exception as e:
            logger.error(f"Failed to write run log: {e}")
