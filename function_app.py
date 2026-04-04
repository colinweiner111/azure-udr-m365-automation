"""Azure Function for automating Azure Route Table updates with M365 endpoints."""

import logging
import os
import azure.functions as func
from typing import List

from shared.m365_api import get_current_version, get_endpoints, extract_ipv4_cidrs
from shared.state_manager import StateManager
from shared.route_manager import RouteTableManager


# Configure structured logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = func.FunctionApp()


@app.schedule(schedule="0 0 0 * * *", arg_name="mytimer", run_on_startup=False,
              use_monitor=False)
def update_m365_routes(mytimer: func.TimerRequest) -> None:
    """
    Timer-triggered function to update Azure Route Tables with M365 endpoints.

    Runs daily (configurable via schedule parameter above).

    Environment variables:
        SUBSCRIPTION_ID: Azure subscription ID
        RESOURCE_GROUP: Resource group containing route tables
        ROUTE_TABLE_NAMES: Comma-separated route table names
        STORAGE_ACCOUNT_NAME: Azure Storage account name
        CONTAINER_NAME: Blob container for state storage
        NEXT_HOP_TYPE: "Internet" or "VirtualAppliance" (default: Internet)
        NEXT_HOP_IP: IP address if NEXT_HOP_TYPE is VirtualAppliance
    """

    # Parse configuration
    config = parse_config()
    if not config:
        logger.error("Failed to parse configuration")
        return

    logger.info("=" * 80)
    logger.info("Starting M365 Route Table Update Function")
    logger.info(f"Route tables: {config['route_table_names']}")
    logger.info(f"Timer trigger: past_due={mytimer.past_due if mytimer else 'manual'}")

    try:
        # Initialize managers
        state_mgr = StateManager(
            config["storage_account_name"],
            config["container_name"]
        )

        route_mgr = RouteTableManager(
            config["subscription_id"],
            config["resource_group"],
            config["route_table_names"],
            config["next_hop_type"],
            config["next_hop_ip"]
        )

        # Fetch M365 endpoints
        logger.info("Fetching M365 endpoints...")
        endpoints = get_endpoints(categories=["Optimize", "Allow"])
        if not endpoints:
            logger.error("Failed to fetch M365 endpoints")
            return

        new_cidrs = extract_ipv4_cidrs(endpoints)
        if not new_cidrs:
            logger.error("No IPv4 CIDRs extracted from endpoints")
            return

        # Get current version
        current_version = get_current_version()
        if current_version is None:
            logger.warning("Could not determine M365 version, proceeding anyway")
        else:
            logger.info(f"M365 version: {current_version}")

        # Calculate diff
        to_add, to_remove = state_mgr.get_diff(new_cidrs)

        # If no changes, exit early
        if not to_add and not to_remove:
            logger.info("No changes detected, exiting")
            return

        logger.info(f"Changes detected: +{len(to_add)} -{len(to_remove)}")

        # Apply changes to route tables
        add_summary = None
        remove_summary = None

        if to_remove:
            logger.info(f"Removing {len(to_remove)} routes...")
            remove_summary = route_mgr.remove_routes(to_remove)

        if to_add:
            logger.info(f"Adding {len(to_add)} routes...")
            add_summary = route_mgr.add_routes(to_add)

        # Save new state
        if state_mgr.save_state(current_version, new_cidrs):
            logger.info("State saved successfully")
        else:
            logger.error("Failed to save state")

        # Log summary
        log_summary(
            current_version,
            len(new_cidrs),
            to_add,
            to_remove,
            add_summary,
            remove_summary
        )

    except Exception as e:
        logger.exception(f"Error in main function: {e}")
        raise


def parse_config() -> dict:
    """Parse and validate environment configuration.

    Returns:
        Configuration dict or None if validation fails.
    """
    config = {
        "subscription_id": os.getenv("SUBSCRIPTION_ID"),
        "resource_group": os.getenv("RESOURCE_GROUP"),
        "route_table_names": [
            name.strip()
            for name in os.getenv("ROUTE_TABLE_NAMES", "").split(",")
            if name.strip()
        ],
        "storage_account_name": os.getenv("STORAGE_ACCOUNT_NAME"),
        "container_name": os.getenv("CONTAINER_NAME"),
        "next_hop_type": os.getenv("NEXT_HOP_TYPE", "Internet"),
        "next_hop_ip": os.getenv("NEXT_HOP_IP"),
    }

    # Validate required settings
    required = [
        "subscription_id",
        "resource_group",
        "storage_account_name",
        "container_name"
    ]

    for key in required:
        if not config[key]:
            logger.error(f"Missing required configuration: {key}")
            return None

    if not config["route_table_names"]:
        logger.error("No route table names specified in ROUTE_TABLE_NAMES")
        return None

    if config["next_hop_type"] not in ["Internet", "VirtualAppliance"]:
        logger.error(
            f"Invalid NEXT_HOP_TYPE: {config['next_hop_type']}"
        )
        return None

    if config["next_hop_type"] == "VirtualAppliance" and not config["next_hop_ip"]:
        logger.error(
            "NEXT_HOP_IP required when NEXT_HOP_TYPE is VirtualAppliance"
        )
        return None

    return config


def log_summary(
    version: int,
    total_cidrs: int,
    to_add: List[str],
    to_remove: List[str],
    add_summary: dict,
    remove_summary: dict
) -> None:
    """Log execution summary."""
    logger.info("=" * 80)
    logger.info("EXECUTION SUMMARY")
    logger.info("=" * 80)
    logger.info(f"M365 Version: {version}")
    logger.info(f"Total CIDRs: {total_cidrs}")
    logger.info(f"Routes to Add: {len(to_add)}")
    logger.info(f"Routes to Remove: {len(to_remove)}")

    if add_summary:
        logger.info(f"Add Results: {add_summary['added']} added, "
                    f"{add_summary['failed']} failed")

    if remove_summary:
        logger.info(f"Remove Results: {remove_summary['removed']} removed, "
                    f"{remove_summary['failed']} failed")

    logger.info("=" * 80)
