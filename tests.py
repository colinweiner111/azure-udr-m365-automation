# Unit tests for Azure UDR M365 Automation

import unittest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime
import json
import os
import sys
from pathlib import Path

# Add shared directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from shared import m365_api, route_manager, state_manager


SUBSCRIPTION_ID = os.environ.get("SUBSCRIPTION_ID", "")
RESOURCE_GROUP = os.environ.get("RESOURCE_GROUP", "")
ROUTE_TABLE_NAME = os.environ.get("ROUTE_TABLE_NAME", "")


class TestM365API(unittest.TestCase):
    """Tests for M365 endpoint API client."""
    
    @patch('shared.m365_api.requests.get')
    def test_get_current_version(self, mock_get):
        """Test retrieving current M365 version."""
        mock_response = Mock()
        mock_response.json.return_value = {"latest": 12345}
        mock_get.return_value = mock_response
        
        result = m365_api.get_current_version()
        
        assert result == 12345
        mock_get.assert_called_once()
    
    @patch('shared.m365_api.requests.get')
    def test_extract_ipv4_cidrs(self, mock_get):
        """Test extracting IPv4 CIDRs from endpoints."""
        endpoints = [
            {
                "id": 1,
                "category": "Optimize",
                "ips": ["13.107.6.152/31", "2620:1ec:4a0:1::/64"]
            },
            {
                "id": 2,
                "category": "Allow",
                "ips": ["52.239.192.0/19"]
            }
        ]
        
        result = m365_api.extract_ipv4_cidrs(endpoints)
        
        assert "13.107.6.152/31" in result
        assert "52.239.192.0/19" in result
        assert "2620:1ec:4a0:1::/64" not in result  # IPv6 should be excluded
        assert len(result) == 2


class TestRouteTableManager(unittest.TestCase):
    """Tests for route table management."""
    
    @patch('shared.route_manager.NetworkManagementClient')
    @patch('shared.route_manager.DefaultAzureCredential')
    def test_generate_route_name(self, mock_cred, mock_client):
        """Test route name generation."""
        manager = route_manager.RouteTableManager(
            "sub-id",
            "rg",
            ["rt1"]
        )
        
        name = manager._generate_route_name("13.107.6.152/31")
        
        assert name == "m365_13_107_6_152_31"
        assert all(c.isalnum() or c == '_' for c in name)


class TestStateManager(unittest.TestCase):
    """Tests for state management."""
    
    @patch('shared.state_manager.DefaultAzureCredential')
    @patch('shared.state_manager.BlobClient.from_blob_url')
    def test_get_diff(self, mock_from_url, mock_cred):
        """Test CIDR diff calculation."""
        # Mock the blob client instance returned by from_blob_url
        mock_client_instance = MagicMock()
        mock_from_url.return_value = mock_client_instance
        
        old_state = {
            "version": 100,
            "cidrs": ["1.0.0.0/8", "2.0.0.0/8"],
            "timestamp": "2024-01-01T00:00:00Z"
        }
        
        mock_client_instance.download_blob.return_value.readall.return_value = \
            json.dumps(old_state).encode()
        
        manager = state_manager.StateManager("account", "container")
        to_add, to_remove = manager.get_diff(
            ["1.0.0.0/8", "3.0.0.0/8"]
        )
        
        assert "3.0.0.0/8" in to_add
        assert "2.0.0.0/8" in to_remove


class TestM365RoutesPreview(unittest.TestCase):
    """
    Fetches live M365 endpoint data and writes a preview CSV of all routes
    this function would create — no Azure credentials required, internet only.

    Outputs:
      m365_routes_preview.csv  — open in Excel to inspect all routes
      m365_routes_preview.log  — step-by-step log of what happened
    """

    def test_fetch_and_write_routes_preview(self):
        """Fetch real M365 CIDRs and write them to m365_routes_preview.csv."""
        import csv
        import ipaddress
        import logging
        import urllib.request
        import uuid
        from datetime import datetime, timezone

        base_path = Path(__file__).parent
        log_path = base_path / "m365_routes_preview.log"
        output_path = base_path / "m365_routes_preview.csv"

        # Set up a logger that writes to both the log file and stdout
        log = logging.getLogger("m365_preview")
        log.setLevel(logging.DEBUG)
        log.handlers.clear()
        fh = logging.FileHandler(log_path, mode="w", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
        log.addHandler(fh)
        log.addHandler(ch)

        log.info("=" * 60)
        log.info("  M365 Routes Preview")
        log.info("  This test shows exactly what the Azure Function would do")
        log.info("  when it runs — no Azure credentials required")
        log.info("=" * 60)
        log.info(f"Started at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        log.info("")

        # Step 1: Fetch version
        log.info("--- Step 1: Check M365 endpoint version ---")
        version_client_id = str(uuid.uuid4())
        version_url = f"https://endpoints.office.com/version/worldwide?clientrequestid={version_client_id}"
        log.info(f"Calling version API: {version_url}")
        with urllib.request.urlopen(version_url, timeout=15) as resp:
            version_data = json.loads(resp.read().decode())
        current_version = version_data.get("latest", "unknown")
        log.info(f"Current M365 endpoint version: {current_version}")
        log.info("(The function compares this against the last stored version.")
        log.info(" If unchanged, it skips the update entirely — no Azure calls made.)")
        log.info("")

        # Step 2: Fetch endpoints
        log.info("--- Step 2: Fetch full endpoint list ---")
        client_id = str(uuid.uuid4())
        url = f"https://endpoints.office.com/endpoints/worldwide?clientrequestid={client_id}"
        log.info(f"Calling endpoints API: {url}")
        with urllib.request.urlopen(url, timeout=15) as resp:
            endpoints = json.loads(resp.read().decode())
        log.info(f"Received {len(endpoints)} endpoint records from Microsoft")
        all_categories = {}
        for ep in endpoints:
            cat = ep.get("category", "unknown")
            all_categories[cat] = all_categories.get(cat, 0) + 1
        log.info("Breakdown of all records by category (before filtering):")
        for cat, count in sorted(all_categories.items()):
            log.info(f"  {cat}: {count} records")
        log.info("")

        # Step 3: Filter and extract IPv4 CIDRs
        log.info("--- Step 3: Filter to 'Optimize' + 'Allow', extract IPv4 CIDRs ---")
        log.info("Reason: 'Default' category traffic is not M365-critical and does not need bypass")
        log.info("Reason: IPv6 routes are skipped — Azure UDRs are IPv4 only in this solution")
        rows = []
        skipped_ipv6 = 0
        skipped_category = 0
        counts_by_category = {}
        counts_by_service = {}

        for ep in endpoints:
            category = ep.get("category", "")
            if category not in ("Optimize", "Allow"):
                skipped_category += 1
                continue
            service_area = ep.get("serviceArea", "")
            for ip in ep.get("ips", []):
                try:
                    net = ipaddress.ip_network(ip, strict=False)
                    if not isinstance(net, ipaddress.IPv4Network):
                        skipped_ipv6 += 1
                        log.debug(f"  Skipping IPv6: {ip}")
                        continue
                except ValueError:
                    log.warning(f"  Skipping invalid CIDR: {ip}")
                    continue
                route_name = f"m365_{ip.replace('.', '_').replace('/', '_')}"
                rows.append({
                    "route_name": route_name,
                    "address_prefix": ip,
                    "next_hop_type": "Internet",
                    "category": category,
                    "service_area": service_area,
                })
                counts_by_category[category] = counts_by_category.get(category, 0) + 1
                counts_by_service[service_area] = counts_by_service.get(service_area, 0) + 1

        log.info(f"Skipped {skipped_category} records (category not Optimize/Allow)")
        log.info(f"Skipped {skipped_ipv6} IPv6 entries")
        log.info(f"Extracted {len(rows)} IPv4 routes to create")
        log.info("")
        log.info("Routes by category:")
        for cat, count in sorted(counts_by_category.items()):
            log.info(f"  {cat}: {count} routes")
        log.info("")
        log.info("Routes by service area:")
        for svc, count in sorted(counts_by_service.items(), key=lambda x: -x[1]):
            log.info(f"  {svc}: {count} routes")
        log.info("")

        # Step 4: Show Azure route table impact
        log.info("--- Step 4: Azure Route Table impact assessment ---")
        azure_limit = 400
        log.info(f"Azure route table limit: {azure_limit} routes per table")
        log.info(f"Total routes this function would create: {len(rows)}")
        if len(rows) > azure_limit:
            tables_needed = -(-len(rows) // azure_limit)  # ceiling division
            log.warning(f"WARNING: {len(rows)} routes exceeds the {azure_limit}-route limit!")
            log.warning(f"You would need at least {tables_needed} route tables to hold all routes.")
            log.warning("Consider filtering to 'Optimize' only, or distributing across multiple subnets.")
        else:
            log.info(f"OK: {len(rows)} routes fits within a single route table (limit: {azure_limit})")
        log.info("")

        # Step 5: Show a sample of what routes look like
        log.info("--- Step 5: Sample of routes that would be created (first 10) ---")
        log.info(f"  {'ROUTE NAME':<40} {'CIDR':<20} {'CATEGORY':<10} SERVICE AREA")
        log.info(f"  {'-'*40} {'-'*20} {'-'*10} {'-'*20}")
        for row in rows[:10]:
            log.info(f"  {row['route_name']:<40} {row['address_prefix']:<20} {row['category']:<10} {row['service_area']}")
        if len(rows) > 10:
            log.info(f"  ... and {len(rows) - 10} more (see CSV for full list)")
        log.info("")

        # Step 6: Write CSV
        log.info("--- Step 6: Write full route list to CSV ---")
        log.info(f"Output file: {output_path}")
        with open(output_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["route_name", "address_prefix", "next_hop_type", "category", "service_area"])
            writer.writeheader()
            writer.writerows(rows)
        log.info(f"CSV written: {len(rows)} routes + header row")
        log.info("Open m365_routes_preview.csv in Excel to browse the full list")
        log.info("")
        log.info("=" * 60)
        log.info(f"  Preview complete. M365 version: {current_version}")
        log.info(f"  {len(rows)} routes would be created across your route tables")
        log.info(f"  Log saved to: {log_path}")
        log.info("=" * 60)

        self.assertGreater(len(rows), 0, "No routes fetched from M365 API")


@unittest.skipIf(
    not (SUBSCRIPTION_ID and RESOURCE_GROUP and ROUTE_TABLE_NAME),
    "Integration tests require SUBSCRIPTION_ID, RESOURCE_GROUP, and ROUTE_TABLE_NAME environment variables"
)
class TestM365LiveIntegration(unittest.TestCase):
    """
    Integration tests using real M365 IPs fetched live from endpoints.office.com.

    Fetches actual IPv4 CIDRs for Teams, Exchange Online, SharePoint Online,
    and OneDrive for Business, adds them to a real Azure route table, verifies
    they are present, then removes them.

    Requires:
      - Active `az login` session
      - SUBSCRIPTION_ID, RESOURCE_GROUP, ROUTE_TABLE_NAME environment variables
      - Network Contributor role on the route table's resource group
    """

    SUBSCRIPTION_ID = SUBSCRIPTION_ID
    RESOURCE_GROUP = RESOURCE_GROUP
    ROUTE_TABLE_NAME = ROUTE_TABLE_NAME

    SERVICE_AREAS = ["Teams", "Exchange", "SharePoint", "OneDrive"]

    @classmethod
    def setUpClass(cls):
        cls.manager = route_manager.RouteTableManager(
            cls.SUBSCRIPTION_ID,
            cls.RESOURCE_GROUP,
            [cls.ROUTE_TABLE_NAME],
            next_hop_type="Internet",
        )

        # Fetch live endpoints filtered to the four service areas
        import urllib.request, uuid
        client_id = str(uuid.uuid4())
        url = f"https://endpoints.office.com/endpoints/worldwide?clientrequestid={client_id}"
        with urllib.request.urlopen(url, timeout=15) as resp:
            all_endpoints = json.loads(resp.read().decode())

        target_areas = {a.lower() for a in cls.SERVICE_AREAS}
        cidrs = set()
        for ep in all_endpoints:
            if ep.get("serviceArea", "").lower() not in target_areas:
                continue
            for ip in ep.get("ips", []):
                if ":" not in ip:   # IPv4 only
                    cidrs.add(ip)

        cls.live_cidrs = sorted(cidrs)
        print(f"\nFetched {len(cls.live_cidrs)} real M365 IPv4 CIDRs for {cls.SERVICE_AREAS}")

    def test_add_real_m365_routes(self):
        """Add live M365 CIDRs and verify they appear in Azure."""
        self.assertGreater(len(self.live_cidrs), 0, "No live CIDRs fetched from M365 API")

        summary = self.manager.add_routes(self.live_cidrs)

        self.assertEqual(summary["failed"], 0, f"Some routes failed to add: {summary}")
        self.assertGreater(summary["added"], 0)

        routes_by_table = self.manager.get_current_routes()
        current = set(routes_by_table[self.ROUTE_TABLE_NAME])

        missing = [c for c in self.live_cidrs if c not in current]
        self.assertEqual(missing, [], f"These CIDRs were not found in Azure after add: {missing}")

    def test_add_real_m365_routes_is_idempotent(self):
        """Adding the same live CIDRs twice should not fail or duplicate."""
        self.manager.add_routes(self.live_cidrs)
        summary = self.manager.add_routes(self.live_cidrs)

        self.assertEqual(summary["added"], 0, "Second add should add 0 (already present)")
        self.assertEqual(summary["failed"], 0)

    def test_remove_real_m365_routes(self):
        """Add live M365 CIDRs then remove them and verify they are gone."""
        self.manager.add_routes(self.live_cidrs)

        summary = self.manager.remove_routes(self.live_cidrs)

        self.assertEqual(summary["failed"], 0, f"Some routes failed to remove: {summary}")
        self.assertGreater(summary["removed"], 0)

        routes_by_table = self.manager.get_current_routes()
        current = set(routes_by_table[self.ROUTE_TABLE_NAME])

        still_present = [c for c in self.live_cidrs if c in current]
        self.assertEqual(still_present, [], f"These CIDRs still present after remove: {still_present}")


@unittest.skipIf(
    not (SUBSCRIPTION_ID and RESOURCE_GROUP and ROUTE_TABLE_NAME),
    "Integration tests require SUBSCRIPTION_ID, RESOURCE_GROUP, and ROUTE_TABLE_NAME environment variables"
)
class TestRouteTableIntegration(unittest.TestCase):
    """
    Integration tests against a real Azure Route Table.

    Requires:
      - Active `az login` session (DefaultAzureCredential uses the CLI token)
            - ROUTE_TABLE_NAME in RESOURCE_GROUP under the current subscription

    These tests CREATE and DELETE real Azure resources.
    Run with: python tests.py TestRouteTableIntegration -v
    """

    SUBSCRIPTION_ID = SUBSCRIPTION_ID
    RESOURCE_GROUP = RESOURCE_GROUP
    ROUTE_TABLE_NAME = ROUTE_TABLE_NAME
    TEST_CIDR = "203.0.113.0/24"   # TEST-NET-3 (RFC 5737) — safe, non-routable

    @classmethod
    def setUpClass(cls):
        """Build a RouteTableManager pointed at the real Azure route table."""
        cls.manager = route_manager.RouteTableManager(
            cls.SUBSCRIPTION_ID,
            cls.RESOURCE_GROUP,
            [cls.ROUTE_TABLE_NAME],
            next_hop_type="Internet",
        )

    def tearDown(self):
        """Always remove the test CIDR after each test so state is clean."""
        try:
            self.manager.remove_routes([self.TEST_CIDR])
        except Exception:
            pass  # Best-effort cleanup

    def test_add_route_creates_entry_in_azure(self):
        """Add a route and verify Azure reflects it."""
        summary = self.manager.add_routes([self.TEST_CIDR])

        self.assertEqual(summary["added"], 1, f"Expected 1 added, got: {summary}")
        self.assertEqual(summary["failed"], 0)

        # Confirm route actually exists in Azure
        routes_by_table = self.manager.get_current_routes()
        cidrs = routes_by_table[self.ROUTE_TABLE_NAME]
        self.assertIn(
            self.TEST_CIDR, cidrs,
            f"{self.TEST_CIDR} not found in Azure route table. Routes: {cidrs}"
        )

    def test_add_route_is_idempotent(self):
        """Adding the same route twice should not fail or duplicate."""
        self.manager.add_routes([self.TEST_CIDR])
        summary = self.manager.add_routes([self.TEST_CIDR])  # second call

        # Second call should add 0 (already exists) and fail 0
        self.assertEqual(summary["added"], 0)
        self.assertEqual(summary["failed"], 0)

    def test_remove_route_deletes_entry_from_azure(self):
        """Add then remove a route and verify it's gone."""
        self.manager.add_routes([self.TEST_CIDR])

        summary = self.manager.remove_routes([self.TEST_CIDR])

        self.assertEqual(summary["removed"], 1, f"Expected 1 removed, got: {summary}")
        self.assertEqual(summary["failed"], 0)

        # Confirm route is gone
        routes_by_table = self.manager.get_current_routes()
        cidrs = routes_by_table[self.ROUTE_TABLE_NAME]
        self.assertNotIn(
            self.TEST_CIDR, cidrs,
            f"{self.TEST_CIDR} still present after removal. Routes: {cidrs}"
        )

    def test_get_current_routes_returns_list(self):
        """get_current_routes should return a dict with the table name as key."""
        routes_by_table = self.manager.get_current_routes()

        self.assertIn(self.ROUTE_TABLE_NAME, routes_by_table)
        self.assertIsInstance(routes_by_table[self.ROUTE_TABLE_NAME], list)


if __name__ == '__main__':
    unittest.main()
