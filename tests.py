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
