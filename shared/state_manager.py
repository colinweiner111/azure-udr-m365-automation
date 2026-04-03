"""State management for version and IP tracking using Azure Blob Storage."""

import json
import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime
from azure.storage.blob import BlobClient
from azure.identity import DefaultAzureCredential

logger = logging.getLogger(__name__)


class StateManager:
    """Manages state persistence in Azure Blob Storage."""
    
    def __init__(
        self,
        storage_account_name: str,
        container_name: str,
        blob_name: str = "m365_route_state.json"
    ):
        """Initialize state manager.
        
        Args:
            storage_account_name: Azure Storage account name.
            container_name: Blob container name.
            blob_name: Name of the state blob (default: m365_route_state.json).
        """
        self.blob_name = blob_name
        self.storage_account_name = storage_account_name
        self.container_name = container_name
        
        account_url = \
            f"https://{storage_account_name}.blob.core.windows.net"
        blob_url = f"{account_url}/{container_name}/{blob_name}"
        
        credential = DefaultAzureCredential()
        self.blob_client = BlobClient.from_blob_url(blob_url, credential)
    
    def get_state(self) -> Dict:
        """Retrieve current state from blob storage.
        
        Returns:
            Dictionary with keys:
            - version: Last processed M365 version
            - cidrs: List of currently managed CIDRs
            - timestamp: ISO 8601 timestamp of last update
        """
        try:
            blob_data = self.blob_client.download_blob().readall()
            state = json.loads(blob_data)
            logger.info(
                f"Loaded state: version={state.get('version')}, "
                f"CIDRs={len(state.get('cidrs', []))}"
            )
            return state
        except Exception as e:
            logger.info(f"No existing state found: {e}")
            return {
                "version": None,
                "cidrs": [],
                "timestamp": None
            }
    
    def save_state(self, version: int, cidrs: List[str]) -> bool:
        """Save state to blob storage.
        
        Args:
            version: Current M365 version.
            cidrs: List of CIDRs currently in route tables.
        
        Returns:
            True if successful, False otherwise.
        """
        try:
            state = {
                "version": version,
                "cidrs": sorted(cidrs),
                "timestamp": datetime.utcnow().isoformat() + "Z"
            }
            blob_data = json.dumps(state, indent=2)
            self.blob_client.upload_blob(blob_data, overwrite=True)
            logger.info(
                f"Saved state: version={version}, "
                f"CIDRs={len(cidrs)}, timestamp={state['timestamp']}"
            )
            return True
        except Exception as e:
            logger.error(f"Failed to save state: {e}")
            return False
    
    def get_diff(
        self,
        new_cidrs: List[str]
    ) -> Tuple[List[str], List[str]]:
        """Calculate diff between stored and new CIDRs.
        
        Args:
            new_cidrs: New list of CIDRs.
        
        Returns:
            Tuple of (to_add, to_remove) CIDR lists.
        """
        current_state = self.get_state()
        old_cidrs = set(current_state.get("cidrs", []))
        new_cidrs_set = set(new_cidrs)
        
        to_add = sorted(list(new_cidrs_set - old_cidrs))
        to_remove = sorted(list(old_cidrs - new_cidrs_set))
        
        logger.info(f"Diff: +{len(to_add)} -{len(to_remove)}")
        return to_add, to_remove
