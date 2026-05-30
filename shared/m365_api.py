"""M365 Endpoint API client."""

import ipaddress
import logging
import os
import requests
from typing import Dict, List, Tuple, Optional

logger = logging.getLogger(__name__)

M365_ENDPOINTS_URL = "https://endpoints.office.com/endpoints/worldwide"
M365_VERSION_URL = "https://endpoints.office.com/version/worldwide"
M365_CHANGES_URL = "https://endpoints.office.com/changes/worldwide"

# Stable client ID required by the M365 endpoints API for all requests.
# Override per-deployment via the M365_CLIENT_REQUEST_ID environment variable
# to avoid rate-limit collisions across unrelated deployments of this function.
_CLIENT_REQUEST_ID = os.environ.get("M365_CLIENT_REQUEST_ID", "f482c14b-4d3c-41e9-a5cd-37eaa8a5cb0e")
_API_PARAMS = {"clientrequestid": _CLIENT_REQUEST_ID}


def get_current_version() -> Optional[int]:
    """Fetch the current version of M365 endpoints.
    
    Returns:
        The version number or None if request fails.
    """
    try:
        response = requests.get(M365_VERSION_URL, params=_API_PARAMS, timeout=10)
        response.raise_for_status()
        return response.json().get("latest")
    except requests.RequestException as e:
        logger.error(f"Failed to fetch M365 version: {e}")
        return None


def get_endpoints(
    categories: Optional[List[str]] = None
) -> List[Dict]:
    """Fetch M365 endpoints.
    
    Args:
        categories: List of categories to filter (e.g., ["Optimize", "Allow"]).
                   If None, fetches all endpoints.
    
    Returns:
        List of endpoint dictionaries.
    """
    try:
        response = requests.get(M365_ENDPOINTS_URL, params=_API_PARAMS, timeout=10)
        response.raise_for_status()
        data = response.json()
        logger.info(f"Fetched {len(data)} endpoint groups from M365 API")
        
        if not categories:
            return data
        
        filtered = [item for item in data if item.get("category") in categories]
        logger.info(
            f"Filtered to {len(filtered)} groups with categories {categories}"
        )
        return filtered
    except requests.RequestException as e:
        logger.error(f"Failed to fetch M365 endpoints: {e}")
        return []


def extract_ipv4_cidrs(endpoints: List[Dict]) -> List[str]:
    """Extract IPv4 CIDRs from endpoint data.
    
    Args:
        endpoints: List of endpoint dictionaries from M365 API.
    
    Returns:
        List of unique, sorted IPv4 CIDR strings.
    """
    cidrs = set()
    
    for endpoint in endpoints:
        ips = endpoint.get("ips", [])
        if not ips:
            continue
        
        for ip in ips:
            try:
                net = ipaddress.ip_network(ip, strict=False)
                if isinstance(net, ipaddress.IPv4Network):
                    cidrs.add(ip)
            except ValueError:
                logger.warning(f"Skipping invalid CIDR: {ip}")
    
    logger.info(f"Extracted {len(cidrs)} unique IPv4 CIDRs")
    return sorted(cidrs)


# TODO: wire this up as an optimization — use /changes endpoint for delta updates
# instead of full sync. See README "Future Enhancements" section.
def get_changes_since_version(version: int) -> Tuple[List[str], List[str]]:
    """Get added and removed changes since a specific version.
    
    Args:
        version: The baseline version to compare against.
    
    Returns:
        Tuple of (added_cidrs, removed_cidrs)
    """
    try:
        response = requests.get(
            f"{M365_CHANGES_URL}({version})",
            params=_API_PARAMS,
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        
        added = []
        removed = []
        
        # Parse additions
        additions = data.get("add", [])
        for item in additions:
            ips = item.get("ips", [])
            for ip in ips:
                try:
                    net = ipaddress.ip_network(ip, strict=False)
                    if isinstance(net, ipaddress.IPv4Network):
                        added.append(ip)
                except ValueError:
                    logger.warning(f"Skipping invalid CIDR in additions: {ip}")

        # Parse removals
        removals = data.get("remove", [])
        for item in removals:
            ips = item.get("ips", [])
            for ip in ips:
                try:
                    net = ipaddress.ip_network(ip, strict=False)
                    if isinstance(net, ipaddress.IPv4Network):
                        removed.append(ip)
                except ValueError:
                    logger.warning(f"Skipping invalid CIDR in removals: {ip}")
        
        logger.info(f"Changes since v{version}: +{len(added)} -{len(removed)}")
        return sorted(list(set(added))), sorted(list(set(removed)))
    except requests.RequestException as e:
        logger.error(f"Failed to fetch M365 changes: {e}")
        return [], []
