"""Azure Function for automating Azure Route Table updates with M365 endpoints."""

import logging
import os
import azure.functions as func
from typing import List

from shared.m365_api import get_current_version, get_endpoints, extract_ipv4_cidrs
from shared.state_manager import StateManager
from shared.route_manager import RouteTableManager
from shared.run_logger import RunLogger


# Configure structured logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = func.FunctionApp()


@app.schedule(schedule="0 0 0 * * *", arg_name="mytimer", run_on_startup=False,
              use_monitor=True)
def update_m365_routes(mytimer: func.TimerRequest) -> None:
    """Timer-triggered daily route sync."""
    _sync_routes()


def _sync_routes() -> None:
    """Core sync logic for the timer trigger."""

    # Parse configuration
    config = parse_config()
    if not config:
        logger.error("Failed to parse configuration")
        return

    run_logger = RunLogger(config["storage_account_name"])

    logger.info("=" * 80)
    logger.info("Starting M365 Route Table Update Function")
    logger.info(f"Route tables: {config['route_table_names']}")

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
        logger.info(f"Fetching M365 endpoints (categories: {config['m365_categories']})...")
        endpoints = get_endpoints(categories=config["m365_categories"])
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

        # Calculate diff against saved state (M365 endpoint changes)
        to_add, to_remove = state_mgr.get_diff(new_cidrs)

        # Detect route table drift: routes that should exist but were deleted
        current_routes_by_table = route_mgr.get_current_routes()
        all_current_routes = set()
        for routes in current_routes_by_table.values():
            all_current_routes.update(routes)

        drifted = sorted(set(new_cidrs) - all_current_routes - set(to_remove))
        if drifted:
            logger.warning(f"Detected {len(drifted)} drifted route(s) missing from route table: {drifted}")
            to_add = sorted(set(to_add) | set(drifted))

        # If no changes and no drift, log and exit early
        if not to_add and not to_remove:
            logger.info("No changes detected, exiting")
            run_logger.write(
                m365_version=current_version,
                total_routes=len(new_cidrs),
                added=[],
                removed=[],
                drift_restored=[],
                add_succeeded=0,
                add_failed=0,
                remove_succeeded=0,
                remove_failed=0,
                result="no_change"
            )
            return

        logger.info(f"Changes detected: +{len(to_add)} -{len(to_remove)} (includes {len(drifted)} drifted)")

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
            remove_summary,
            drifted
        )

        run_logger.write(
            m365_version=current_version,
            total_routes=len(new_cidrs),
            added=to_add,
            removed=to_remove,
            drift_restored=drifted,
            add_succeeded=add_summary["added"] if add_summary else 0,
            add_failed=add_summary["failed"] if add_summary else 0,
            remove_succeeded=remove_summary["removed"] if remove_summary else 0,
            remove_failed=remove_summary["failed"] if remove_summary else 0,
            result="success"
        )

    except Exception as e:
        logger.exception(f"Error in main function: {e}")
        run_logger.write(
            m365_version=None,
            total_routes=0,
            added=[],
            removed=[],
            drift_restored=[],
            add_succeeded=0,
            add_failed=0,
            remove_succeeded=0,
            remove_failed=0,
            result="error",
            error=str(e)
        )
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
        "m365_categories": [
            c.strip()
            for c in os.getenv("M365_CATEGORIES", "Optimize,Allow").split(",")
            if c.strip()
        ],
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
    remove_summary: dict,
    drifted: List[str] = None
) -> None:
    """Log execution summary."""
    drifted = drifted or []
    drifted_set = set(drifted)
    m365_new = [r for r in to_add if r not in drifted_set]

    logger.info("=" * 80)
    logger.info("EXECUTION SUMMARY")
    logger.info("=" * 80)
    logger.info(f"M365 Version:    {version}")
    logger.info(f"Total CIDRs:     {total_cidrs}")
    logger.info(f"Routes Added:    {len(to_add)} ({len(drifted)} drift restores, {len(m365_new)} new from M365)")
    logger.info(f"Routes Removed:  {len(to_remove)} (retired from M365)")

    if drifted:
        logger.info(f"  Drift restored:  {', '.join(drifted)}")
    if m365_new:
        logger.info(f"  New M365 routes: {', '.join(m365_new)}")
    if to_remove:
        logger.info(f"  Removed routes:  {', '.join(to_remove)}")

    if add_summary:
        logger.info(f"Add result:      {add_summary['added']} succeeded, {add_summary['failed']} failed")
        if add_summary.get('failed'):
            for table, t in add_summary.get('tables', {}).items():
                for err in t.get('errors', []):
                    logger.error(f"  [{table}] {err}")

    if remove_summary:
        logger.info(f"Remove result:   {remove_summary['removed']} succeeded, {remove_summary['failed']} failed")
        if remove_summary.get('failed'):
            for table, t in remove_summary.get('tables', {}).items():
                for err in t.get('errors', []):
                    logger.error(f"  [{table}] {err}")

    logger.info("=" * 80)
