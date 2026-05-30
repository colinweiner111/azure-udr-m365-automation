"""Azure Route Table management."""

import logging
from typing import List, Dict, Any
from azure.mgmt.network import NetworkManagementClient
from azure.identity import DefaultAzureCredential
from azure.mgmt.network.models import Route

logger = logging.getLogger(__name__)

# Azure Route Table limits and constraints
MAX_ROUTES_PER_TABLE = 400


class RouteTableManager:
    """Manages Azure Route Tables, optionally across multiple resource groups."""

    def __init__(
        self,
        subscription_id: str,
        resource_group: str,
        route_table_names: List[str],
        next_hop_type: str = "Internet",
        next_hop_ip: str = None
    ):
        """
        Args:
            subscription_id: Azure subscription ID (all route tables must be in the same subscription).
            resource_group: Default resource group; used for any entry in route_table_names that
                            does not include an explicit RG prefix (``rg/tablename`` format).
            route_table_names: Table names to manage.  Each entry may be either a bare table name
                               (uses ``resource_group``) or a ``<resource-group>/<table-name>`` pair
                               to target a table in a different resource group.
                               Example: ``["rg-hub/rt-hub", "rg-spoke1/rt-spoke1", "rt-legacy"]``
            next_hop_type: ``Internet`` or ``VirtualAppliance``.
            next_hop_ip: NVA private IP; required when next_hop_type is ``VirtualAppliance``.
        """
        credential = DefaultAzureCredential()
        self.client = NetworkManagementClient(credential, subscription_id)
        self.subscription_id = subscription_id
        self.default_resource_group = resource_group
        self.next_hop_type = next_hop_type
        self.next_hop_ip = next_hop_ip

        # Resolve each entry into a (resource_group, table_name) tuple.
        self.route_tables: List[tuple] = []
        for entry in route_table_names:
            entry = entry.strip()
            if "/" in entry:
                rg, tbl = entry.split("/", 1)
                rg = rg.strip()
                tbl = tbl.strip()
                if not rg or not tbl:
                    raise ValueError(
                        f"Invalid ROUTE_TABLE_NAMES entry '{entry}': expected 'resourcegroup/tablename' "
                        "or a bare table name."
                    )
                self.route_tables.append((rg, tbl))
            else:
                if not entry:
                    raise ValueError(
                        "Invalid ROUTE_TABLE_NAMES entry: empty table name is not allowed."
                    )
                self.route_tables.append((resource_group, entry))

        # Keep route_table_names as a property for backwards-compatible logging.
        self.route_table_names = [tbl for _, tbl in self.route_tables]

        if next_hop_type == "VirtualAppliance" and not next_hop_ip:
            raise ValueError("next_hop_ip required when next_hop_type is VirtualAppliance")

        logger.info(f"RouteTableManager initialized for tables: {self.route_tables}, next_hop: {next_hop_type}")

    def get_current_routes(self) -> Dict[str, List[str]]:
        routes_by_table = {}
        for rg, table_name in self.route_tables:
            key = f"{rg}/{table_name}"
            try:
                route_table = self.client.route_tables.get(rg, table_name)
                routes = []
                if route_table.routes:
                    for route in route_table.routes:
                        if route.address_prefix:
                            routes.append(route.address_prefix)
                routes_by_table[key] = sorted(routes)
                logger.info(f"Retrieved {len(routes)} routes from {key}")
            except Exception as e:
                logger.error(f"Failed to retrieve routes from {key}: {e}")
                routes_by_table[key] = []
        return routes_by_table

    def add_routes(self, cidrs: List[str]) -> Dict[str, Any]:
        summary = {"total_cidrs": len(cidrs), "added": 0, "failed": 0, "tables": {}}
        if not cidrs:
            logger.info("No routes to add")
            return summary

        for rg, table_name in self.route_tables:
            key = f"{rg}/{table_name}"
            table_summary = {
                "added": 0,
                "failed": 0,
                "errors": [],
                "added_routes": [],
                "failed_routes": [],
            }
            try:
                route_table = self.client.route_tables.get(rg, table_name)
                current_count = len(route_table.routes) if route_table.routes else 0

                if current_count >= MAX_ROUTES_PER_TABLE:
                    msg = f"Route table {key} at capacity ({current_count}/{MAX_ROUTES_PER_TABLE})"
                    logger.warning(msg)
                    table_summary["errors"].append(msg)
                    summary["tables"][key] = table_summary
                    continue

                existing_prefixes = set()
                if route_table.routes:
                    existing_prefixes = {r.address_prefix for r in route_table.routes}

                for cidr in cidrs:
                    if cidr in existing_prefixes:
                        logger.debug(f"Route {cidr} already exists in {key}")
                        continue
                    if current_count >= MAX_ROUTES_PER_TABLE:
                        msg = f"Reached route limit in {key}, skipping {cidr}"
                        logger.warning(msg)
                        table_summary["errors"].append(msg)
                        table_summary["failed"] += 1
                        break
                    try:
                        route_name = self._generate_route_name(cidr)
                        route = Route(
                            name=route_name,
                            address_prefix=cidr,
                            next_hop_type=self.next_hop_type,
                            next_hop_ip_address=self.next_hop_ip
                        )
                        poller = self.client.routes.begin_create_or_update(
                            rg, table_name, route_name, route
                        )
                        poller.result()
                        table_summary["added"] += 1
                        table_summary["added_routes"].append(cidr)
                        current_count += 1
                        logger.debug(f"Added route {cidr} to {key}")
                    except Exception as e:
                        logger.error(f"Failed to add route {cidr} to {key}: {e}")
                        table_summary["failed"] += 1
                        table_summary["errors"].append(str(e))
                        table_summary["failed_routes"].append(
                            {"cidr": cidr, "error": str(e)}
                        )
            except Exception as e:
                logger.error(f"Failed to process route table {key}: {e}")
                table_summary["errors"].append(str(e))

            summary["tables"][key] = table_summary
            summary["added"] += table_summary["added"]
            summary["failed"] += table_summary["failed"]

        logger.info(f"Route addition summary: {summary}")
        return summary

    def remove_routes(self, cidrs: List[str]) -> Dict[str, Any]:
        summary = {"total_cidrs": len(cidrs), "removed": 0, "failed": 0, "tables": {}}
        if not cidrs:
            logger.info("No routes to remove")
            return summary

        cidrs_set = set(cidrs)
        for rg, table_name in self.route_tables:
            key = f"{rg}/{table_name}"
            table_summary = {
                "removed": 0,
                "failed": 0,
                "errors": [],
                "removed_routes": [],
                "failed_routes": [],
            }
            try:
                route_table = self.client.route_tables.get(rg, table_name)
                if not route_table.routes:
                    summary["tables"][key] = table_summary
                    continue
                for route in route_table.routes:
                    if route.address_prefix not in cidrs_set:
                        continue
                    try:
                        poller = self.client.routes.begin_delete(
                            rg, table_name, route.name
                        )
                        poller.result()
                        table_summary["removed"] += 1
                        table_summary["removed_routes"].append(route.address_prefix)
                        logger.debug(f"Removed route {route.address_prefix} from {key}")
                    except Exception as e:
                        logger.error(f"Failed to remove route {route.name} from {key}: {e}")
                        table_summary["failed"] += 1
                        table_summary["errors"].append(str(e))
                        table_summary["failed_routes"].append(
                            {"cidr": route.address_prefix, "error": str(e)}
                        )
            except Exception as e:
                logger.error(f"Failed to process route table {key}: {e}")
                table_summary["errors"].append(str(e))

            summary["tables"][key] = table_summary
            summary["removed"] += table_summary["removed"]
            summary["failed"] += table_summary["failed"]

        logger.info(f"Route removal summary: {summary}")
        return summary

    @staticmethod
    def _generate_route_name(cidr: str) -> str:
        safe_name = cidr.replace(".", "_").replace("/", "_")
        route_name = f"m365_{safe_name}"
        if len(route_name) > 80:
            raise ValueError(f"Generated route name '{route_name}' exceeds Azure's 80-character limit")
        return route_name
