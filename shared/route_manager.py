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
    """Manages Azure Route Tables."""

    def __init__(
        self,
        subscription_id: str,
        resource_group: str,
        route_table_names: List[str],
        next_hop_type: str = "Internet",
        next_hop_ip: str = None
    ):
        credential = DefaultAzureCredential()
        self.client = NetworkManagementClient(credential, subscription_id)
        self.subscription_id = subscription_id
        self.resource_group = resource_group
        self.route_table_names = route_table_names
        self.next_hop_type = next_hop_type
        self.next_hop_ip = next_hop_ip

        if next_hop_type == "VirtualAppliance" and not next_hop_ip:
            raise ValueError("next_hop_ip required when next_hop_type is VirtualAppliance")

        logger.info(f"RouteTableManager initialized for tables: {route_table_names}, next_hop: {next_hop_type}")

    def get_current_routes(self) -> Dict[str, List[str]]:
        routes_by_table = {}
        for table_name in self.route_table_names:
            try:
                route_table = self.client.route_tables.get(self.resource_group, table_name)
                routes = []
                if route_table.routes:
                    for route in route_table.routes:
                        if route.address_prefix:
                            routes.append(route.address_prefix)
                routes_by_table[table_name] = sorted(routes)
                logger.info(f"Retrieved {len(routes)} routes from {table_name}")
            except Exception as e:
                logger.error(f"Failed to retrieve routes from {table_name}: {e}")
                routes_by_table[table_name] = []
        return routes_by_table

    def add_routes(self, cidrs: List[str]) -> Dict[str, Any]:
        summary = {"total_cidrs": len(cidrs), "added": 0, "failed": 0, "tables": {}}
        if not cidrs:
            logger.info("No routes to add")
            return summary

        for table_name in self.route_table_names:
            table_summary = {"added": 0, "failed": 0, "errors": []}
            try:
                route_table = self.client.route_tables.get(self.resource_group, table_name)
                current_count = len(route_table.routes) if route_table.routes else 0

                if current_count >= MAX_ROUTES_PER_TABLE:
                    msg = f"Route table {table_name} at capacity ({current_count}/{MAX_ROUTES_PER_TABLE})"
                    logger.warning(msg)
                    table_summary["errors"].append(msg)
                    summary["tables"][table_name] = table_summary
                    continue

                existing_prefixes = set()
                if route_table.routes:
                    existing_prefixes = {r.address_prefix for r in route_table.routes}

                for cidr in cidrs:
                    if cidr in existing_prefixes:
                        logger.debug(f"Route {cidr} already exists in {table_name}")
                        continue
                    if current_count >= MAX_ROUTES_PER_TABLE:
                        msg = f"Reached route limit in {table_name}, skipping {cidr}"
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
                            self.resource_group, table_name, route_name, route
                        )
                        poller.result()
                        table_summary["added"] += 1
                        current_count += 1
                        logger.debug(f"Added route {cidr} to {table_name}")
                    except Exception as e:
                        logger.error(f"Failed to add route {cidr} to {table_name}: {e}")
                        table_summary["failed"] += 1
                        table_summary["errors"].append(str(e))
            except Exception as e:
                logger.error(f"Failed to process route table {table_name}: {e}")
                table_summary["errors"].append(str(e))

            summary["tables"][table_name] = table_summary
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
        for table_name in self.route_table_names:
            table_summary = {"removed": 0, "failed": 0, "errors": []}
            try:
                route_table = self.client.route_tables.get(self.resource_group, table_name)
                if not route_table.routes:
                    summary["tables"][table_name] = table_summary
                    continue
                for route in route_table.routes:
                    if route.address_prefix not in cidrs_set:
                        continue
                    try:
                        poller = self.client.routes.begin_delete(
                            self.resource_group, table_name, route.name
                        )
                        poller.result()
                        table_summary["removed"] += 1
                        logger.debug(f"Removed route {route.address_prefix} from {table_name}")
                    except Exception as e:
                        logger.error(f"Failed to remove route {route.name} from {table_name}: {e}")
                        table_summary["failed"] += 1
                        table_summary["errors"].append(str(e))
            except Exception as e:
                logger.error(f"Failed to process route table {table_name}: {e}")
                table_summary["errors"].append(str(e))

            summary["tables"][table_name] = table_summary
            summary["removed"] += table_summary["removed"]
            summary["failed"] += table_summary["failed"]

        logger.info(f"Route removal summary: {summary}")
        return summary

    @staticmethod
    def _generate_route_name(cidr: str) -> str:
        safe_name = cidr.replace(".", "_").replace("/", "_")
        route_name = f"m365_{safe_name}"
        assert len(route_name) <= 80, (
            f"Generated route name '{route_name}' exceeds Azure's 80-character limit"
        )
        return route_name
