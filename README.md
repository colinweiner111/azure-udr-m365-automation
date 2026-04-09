# Azure UDR M365 Automation

Automates updating Azure Route Tables (User-Defined Routes) with Microsoft 365 endpoint IP ranges using a timer-triggered Azure Function.

**Table of Contents**
- [Challenge & Solution](#challenge--solution)
- [Overview](#overview)
- [Architecture](#architecture)
- [Quick Start](#quick-start-3-steps)
- [Security Appliance Integration](#security-appliance-integration)
- [Route Limits & Constraints](#route-limits--constraints)
- [M365 Endpoint API Details](#m365-endpoint-api-details)
- [Prerequisites](#prerequisites)
- [Testing](#testing)
- [Configuration](#configuration)
- [Deployment](#deployment)
- [Known Limitations](#known-limitations)
- [Troubleshooting](#troubleshooting)
- [Future Enhancements](#future-enhancements)

## Challenge & Solution

### The Challenge: Security Appliance & M365 Traffic

You've deployed a **security appliance** (e.g., Zscaler Cloud Connector or similar NVA) in Azure to inspect HTTP/S traffic. It sits inline in your VNet behind an internal load balancer, and your Route Tables point to it as the next hop for internet-bound traffic.

**The situation:** M365 traffic (Teams, Exchange Online, SharePoint Online, etc.) is being forced through the security appliance for inspection, which causes:
- ❌ **Unnecessary latency**: M365 is already trusted; inspection adds delay
- ❌ **Bandwidth waste**: Inspection of traffic that doesn't need security filtering
- ❌ **Performance degradation**: Teams calls, file uploads, email delays
- ❌ **Manual overhead**: M365 publishes 2,000+ IP ranges that change frequently—tracking them manually is error-prone

**Goal:** Route M365 traffic **directly to the Internet** (bypass the security appliance), while routing all other traffic through it for inspection.

### The Solution

This Azure Function maintains UDRs for M365 bypass:

1. **Fetches** the latest M365 endpoint data from the official Microsoft API (daily)
2. **Extracts** IPv4 CIDR blocks from "Optimize" + "Allow" categories (~2,000 IPs)
   ([Microsoft 365 URLs and IP address ranges](https://learn.microsoft.com/en-us/microsoft-365/enterprise/urls-and-ip-address-ranges?view=o365-worldwide))
3. **Compares** against previously stored routes (built-in deduplication)
4. **Creates UDRs** pointing M365 IPs to next hop = `Internet` (bypasses the security appliance)
5. **Removes stale routes** when Microsoft retires old IP ranges
6. **Tracks state** in Azure Blob Storage for idempotent operations (no duplicates)
7. **Logs all changes** for audit and compliance

**Result:** M365 traffic bypasses security appliance inspection with no manual updates required. Routes stay synchronized as Microsoft adds/removes IPs.

**Example Scenario:**
A spoke VNet with 200 VMs using a default route to a security appliance ILB (e.g., Zscaler Cloud Connector). After deployment, Teams and Exchange traffic bypass the appliance while all other traffic remains inspected. Administrators get daily digests of IP changes via function logs.

⚠️ **When NOT to use this solution:**
- If your architecture supports FQDN-based filtering (e.g., proxy/PAC files or security appliance URL policies such as Zscaler), that is the preferred Microsoft approach for M365 traffic.
- Use this solution only when IP-based routing is required (e.g., UDR-only architectures, appliance routing constraints).

**Traffic Flow:**
```
VM → Route for M365 IP (next hop: Internet) → Directly to M365 ✓ Fast, no inspection
VM → Route for other IPs (next hop: security appliance) → Security Appliance → Internet ✓ Inspected
```

## Overview

This solution periodically fetches Microsoft 365 endpoint data from the [official M365 IP web service](https://learn.microsoft.com/en-us/microsoft-365/enterprise/microsoft-365-ip-web-service?view=o365-worldwide), extracts IPv4 CIDR blocks, and maintains Azure Route Tables with the latest IP ranges. It uses version tracking and state management to minimize unnecessary route updates.

By default (`function_app.py` line 69), the function fetches both **"Optimize"** and **"Allow"** categories. This is configurable — see [Why "Optimize" + "Allow"?](#why-optimize--allow) below.

### Why "Optimize" + "Allow"?

- **Optimize** → Required for performance (Teams, Exchange Online, SharePoint Online)
  - These services require direct connectivity for real-time communication
- **Allow** → Broader coverage but increases route count
  - Additional Microsoft services and integrations
  - Includes CDN IPs and federated endpoints

Optimize alone is typically much smaller (~18 prefixes as of April 2026), while Optimize + Allow expands to ~2,000+ prefixes.

### Key Features

- **Timer-triggered execution** (configurable schedule, default: daily)
- **Version-aware synchronization** using M365 endpoint version API
- **State tracking** via Azure Blob Storage for idempotent updates
- **Automatic diff calculation** (routes to add/remove)
- **Multi-table support** with configurable route tables
- **Managed Identity authentication** (DefaultAzureCredential)
- **Production-safe** route limits and error handling
- **Structured logging** for audit and troubleshooting

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Timer Trigger (Daily)                   │
└────────────────┬────────────────────────────────────────────┘
                 │
                 ▼
    ┌──────────────────────────────┐
    │  Azure Function (Python)     │
    │  - Timer scheduled event     │
    └──────────────┬───────────────┘
                   │
        ┌──────────┴──────────┐
        │                     │
        ▼                     ▼
  ┌──────────────┐    ┌──────────────────────┐
  │  M365 API    │    │  State Manager       │
  │              │    │  (Blob Storage)      │
  │ endpoints/   │    │                      │
  │ worldwide    │    │  - Last version      │
  │ version      │    │  - Last CIDR list    │
  │ changes      │    │  - Timestamp         │
  └──────┬───────┘    └──────────────────────┘
         │
         │ Extract IPv4 CIDR ranges
         │
         ▼
  ┌──────────────────────────────┐
  │  Route Diff Logic            │
  │  - Compare old ↔ new CIDRs   │
  │  - Calculate add/remove sets │
  └──────┬───────────────────────┘
         │
    ┌────┴────┐
    │          │
    ▼          ▼
┌──────────┐  ┌──────────┐
│ Add      │  │ Remove   │
│ Routes   │  │ Routes   │
└──┬───────┘  └──┬───────┘
   │             │
   └────┬────────┘
        │
        ▼
  ┌──────────────────────────────┐
  │  Azure Route Tables          │
  │                              │
  │  Route Table 1               │
  │  Route Table 2 (optional)    │
  │  Route Table N (optional)    │
  └──────────────────────────────┘
```

## Project Structure

```
azure-udr-m365-automation/
├── function_app/
│   ├── __init__.py              # Entry point
│   ├── function_app.py          # Main function logic
│   └── function.json            # Timer schedule definition (v1 model)
├── shared/
│   ├── __init__.py
│   ├── m365_api.py              # M365 endpoint API client
│   ├── state_manager.py         # Blob Storage state management
│   └── route_manager.py         # Azure Route Table operations
├── infra/
│   ├── main.bicep               # Bicep IaC template
│   └── main.parameters.json     # Deployment parameters
├── tests.py                     # Unit and integration tests
├── requirements.txt             # Python dependencies
└── README.md                    # This file
```

## Quick Start: 3 Steps

### Step 1: Run Tests (Verify Code Works)

```bash
# Install dependencies
pip install -r requirements-dev.txt

# Run all tests (unit + integration against real Azure)
python -m pytest tests.py -v
```

**What happens:**
- 4 unit tests validate logic in isolation (mocked Azure calls)
- 3 live integration tests fetch real M365 IPs and round-trip them through Azure
- 4 integration tests create/delete a real route in Azure and verify end-to-end
- All 11 tests should pass ✅ (integration tests skipped if env vars not set)

### Step 2: Configure Deployment Parameters

Edit `infra/main.parameters.json` with your environment values:

| Parameter | Example | Required |
|-----------|---------|----------|
| `functionAppName` | `udr-m365-automation` | ✅ |
| `storageAccountName` | `udram365abc123` | ✅ (globally unique) |
| `routeTableNames` | `rt-spoke1,rt-spoke2` | ✅ |
| `nextHopType` | `Internet` | Optional (default: `Internet`) |
| `nextHopIp` | `10.0.0.4` | ✅ if `nextHopType` is `VirtualAppliance` |

**Why `nextHopType = Internet`?**
Routes M365 traffic **directly to the Internet**, bypassing your security appliance. All other traffic continues to route through it for inspection.

### Step 3: Deploy to Azure

**Option A: Bicep + func publish (Recommended)**

```bash
# Provision all infrastructure
az group create --name <resource-group> --location <location>
az deployment group create \
  --resource-group <resource-group> \
  --template-file infra/main.bicep \
  --parameters infra/main.parameters.json

# Deploy function code
func azure functionapp publish <function-app-name> --python
```

**Result:** Function runs automatically on schedule (default: `0 0 0 * * *` — midnight UTC daily). Monitor logs:

```bash
az webapp log tail --resource-group <resource-group> --name <function-app-name>
```

**Option B: Manual CLI** (see [Alternative: Manual Deployment](#alternative-manual-deployment) below)

---

## Security Appliance Integration

### Architecture Overview

**Typical Security Appliance + M365 Setup** (e.g., Zscaler Cloud Connector or similar NVA):

```
┌──────────────────────────────────────────────────────────┐
│                     Azure VNet                           │
│                                                          │
│  ┌─────────────────┐         ┌──────────────────────┐  │
│  │   Azure VM      │         │ Internal Load        │  │
│  │   Subnet        │         │ Balancer (ILB)       │  │
│  │                 │         │                      │  │
│  │ UDRs:           │────────▶│ Security Appliance   │  │
│  │ - Default:      │         │ (VM / NVA)           │  │
│  │   next hop =    │         │                      │  │
│  │   appliance ILB │         │ Forwards to Internet │  │
│  │                 │         │ (inspected)          │  │
│  │ - M365 CIDRs:   │         └──────────────────────┘  │
│  │   next hop =    │                                    │
│  │   Internet      │                                    │
│  │   (↓ This Fn) ◄─┼────────┐                         │
│  └─────────────────┘         │                          │
│                              │                          │
│               ┌──────────────┴─────────────┐           │
│               │                            │           │
│               ▼                            ▼           │
│        ┌────────────────┐         ┌──────────────┐   │
│        │ M365 Traffic   │         │ Other IPs    │   │
│        │                │         │              │   │
│        │ Teams          │         │ SAP, Salesforce│  │
│        │ Exchange       │         │ Internal apps│   │
│        │ SharePoint     │         │              │   │
│        │                │         │ (goes via    │   │
│        │ (direct, fast) │         │  appliance) │   │
│        └────────────────┘         └──────────────┘   │
│             └──────────────┬──────────────┘           │
│                            ▼                          │
│                   ┌──────────────────┐               │
│                   │   Internet       │               │
│                   └──────────────────┘               │
│                                                      │
└──────────────────────────────────────────────────────────┘
```

### How It Works

1. **Default Route Table** (all subnets): Next hop = security appliance ILB
   - All traffic routes through the security appliance for inspection

2. **M365 Route Table** OR **M365 routes in existing table**: Next hop = Internet
   - M365 IPs bypass the security appliance, go directly to internet
   - **This function creates/maintains these routes automatically**

### Setup Steps

For each subnet that needs M365 bypass:

If your subnet already has a route table (for example, `0.0.0.0/0` to the security appliance ILB), add M365 routes to that existing table rather than creating a new one. Re-associating a subnet to a different route table replaces the previous association.

```bash
# Create a route table for M365 bypass (only if one does not already exist)
az network route-table create \
  --resource-group $RESOURCE_GROUP \
  --name rt-m365-bypass

# Associate with subnet(s)
az network vnet subnet update \
  --resource-group $RESOURCE_GROUP \
  --vnet-name $VNET_NAME \
  --name $SUBNET_NAME \
  --route-table rt-m365-bypass

# Now set ROUTE_TABLE_NAMES when deploying this function
export ROUTE_TABLE_NAMES="rt-m365-bypass"
```

**Then deploy this function** to populate the M365 routes automatically.

### Route Priority

Azure uses **longest prefix match** for routing:
- M365 route `/32` or `/24` (specific): matches first → goes to Internet
- Default route `0.0.0.0/0`: matches if no more specific route → goes to security appliance

✓ M365 IPs automatically bypass the security appliance  
✓ Everything else goes through the security appliance  

### Verification

After function runs:

```bash
# List routes in the M365 bypass table
az network route-table route list \
  --resource-group $RESOURCE_GROUP \
  --route-table-name rt-m365-bypass \
  --query "[].{name:name, prefix:addressPrefix, nextHop:nextHopType}" \
  -o table

# Expected output:
# NAME                     PREFIX              NEXT HOP
# m365_13_107_6_152_31     13.107.6.152/31     Internet
# m365_13_107_9_152_31     13.107.9.152/31     Internet
# m365_20_190_128_0_18     20.190.128.0/18     Internet
# ...(~2,000 prefixes (each becomes a route))
```

---

## Route Limits & Constraints

🚨 **Azure Route Table Limit:** ~400 routes per table

**The Problem:**
- M365 publishes **2,000+ IPv4 prefixes** (Optimize + Allow combined)
- Your route table can only hold **~400 routes**
- This mismatch requires planning

**Solutions:**

1. **Multiple Route Tables** (Recommended)
   - Create separate route tables for M365 and other traffic
   - Associate different subnets with different tables
   - To scale beyond 400 routes, distribute workloads across multiple subnets, each with its own route table.

2. **Filter to Optimize Only**
  - Requires code change: modify `M365_API_CATEGORIES` in `function_app.py`
  - Typically falls within the ~400 route table limit, but varies; verify after deployment
  - As of April 2026, a live check of the worldwide endpoint API returned 18 unique IPv4 Optimize prefixes
  - Trade-off: Less comprehensive coverage than using Optimize + Allow

3. **Manual Curation**
   - Manually maintain a list of critical M365 IPs only
   - Override `ROUTE_TABLE_NAMES` with hand-picked prefixes
   - Risk: Missing IP ranges if Microsoft adds new CIDR blocks

**Future Enhancement:** Route summarization via RFC 4632 CIDR aggregation could reduce M365 from 2,000+ routes to ~50, but Azure networking APIs do not yet support this natively.

---

## M365 Endpoint API Details

### Official API Reference

Microsoft publishes M365 endpoint data through a free, unauthenticated REST API:

| Endpoint | URL | Purpose |
|----------|-----|---------|
| **Endpoints** | `https://endpoints.office.com/endpoints/worldwide?clientrequestid=<uuid>` | Full list of all M365 IP ranges and URLs, categorized |
| **Version** | `https://endpoints.office.com/version/worldwide?clientrequestid=<uuid>` | Latest version number — check this before calling Endpoints to avoid unnecessary syncs |
| **Changes** | `https://endpoints.office.com/changes/worldwide/<version>?clientrequestid=<uuid>` | Delta of IPs added/removed since a specific version |

> **`clientrequestid`** — A stable UUID you generate once and reuse on every call. Microsoft uses it for telemetry and rate-limit tracking. It does **not** authenticate the caller. The API requires this parameter; requests without it return `400 Bad Request`.

**How this function uses the API:**
1. Calls `/version` to get the current version number
2. Compares against the version stored in blob state — if unchanged, skips the update entirely
3. If changed (or first run), calls `/endpoints` and filters to `category = Optimize` or `Allow`
4. Extracts all `ips` arrays (IPv4 only, skipping any entry containing `:` for IPv6), deduplicates, and sorts

### API Response Example

The Microsoft 365 endpoints API returns data like:

```json
[
  {
    "id": 1,
    "serviceArea": "Exchange",
    "category": "Optimize",
    "required": true,
    "ips": [
      "13.107.6.152/31",
      "13.107.9.152/31",
      "40.103.0.0/16"
    ],
    "urls": [
      "*.mail.protection.outlook.com"
    ]
  },
  {
    "id": 2,
    "serviceArea": "Teams",
    "category": "Allow",
    "required": false,
    "ips": [
      "52.112.0.0/14",
      "52.120.0.0/14"
    ],
    "urls": [
      "*.teams.microsoft.com"
    ]
  }
]
```

👉 **This function extracts the `ips` array, filters by `category` (Optimize/Allow), and converts each CIDR into a UDR entry.**

### Version Tracking

The API includes a version number that changes when Microsoft publishes new IPs:

```
GET https://endpoints.office.com/version?clientrequestid=<uuid>
Response: {"latest": "2024031902"}
```

The function compares versions to avoid unnecessary route updates (optimization).

### Update Frequency & Polling Strategy

Microsoft does not publish M365 endpoint changes on a strict schedule.

- Updates are typically released **monthly**
- Additional **out-of-band changes** can occur at any time (e.g., incidents, service updates)

Microsoft recommends polling the `/version` endpoint **approximately once per hour** to detect changes:
https://learn.microsoft.com/en-us/microsoft-365/enterprise/managing-office-365-endpoints

This solution follows a version-based (event-driven) approach:

1. Call `/version` to check the latest version
2. Compare with the previously stored version
3. Only call `/endpoints` and update routes if the version has changed

This ensures:
- No unnecessary route updates
- Minimal API usage
- Safe handling of out-of-band changes

> **Note:** The `/version` endpoint is lightweight, so frequent polling has minimal overhead.  
> In practice, many environments poll less frequently (e.g., every 4–24 hours), but hourly polling aligns with Microsoft guidance.

---

## Prerequisites

1. **Azure Subscription** with at least one Route Table
2. **Azure Storage Account** with a Blob Container (for state storage)
3. **Azure Function App** (Python 3.11+)
4. **Managed Identity** (User-Assigned or System-Assigned) with required RBAC roles

## Required RBAC Roles

Assign these roles to the function's Managed Identity:

| Role | Scope | Purpose |
|------|-------|---------|
| **Network Contributor** | Resource Group or Route Tables | Create/update/delete routes |
| **Storage Blob Data Contributor** | Storage Account or Container | Read/write state blob |

### Assign roles:

```bash
# Get function identity
FUNCTION_PRINCIPAL_ID=$(az functionapp identity show \
  --resource-group $RESOURCE_GROUP \
  --name $FUNCTION_APP_NAME \
  --query principalId -o tsv)

# Network Contributor on Resource Group
az role assignment create \
  --assignee-object-id $FUNCTION_PRINCIPAL_ID \
  --role "Network Contributor" \
  --scope /subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RESOURCE_GROUP

# Storage Blob Data Contributor
az role assignment create \
  --assignee-object-id $FUNCTION_PRINCIPAL_ID \
  --role "Storage Blob Data Contributor" \
  --scope /subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.Storage/storageAccounts/$STORAGE_ACCOUNT_NAME
```

---

## Known Limitations

⚠️ **Before deploying, understand these constraints:**

| Limitation | Impact | Workaround |
|---|---|---|
| **No IPv6 support** | M365 IPv6 traffic not routed | Microsoft not yet mandating IPv6 for M365 |
| **Route table limit** (~400 routes) | Cannot fit all 2,000+ M365 IPs in one table | Use multiple route tables or filter to "Optimize" only |
| **No route summarization** | 2,000+ distinct routes instead of ~50 | Azure limitation; CIDR aggregation not available |
| **UDR propagation delay** | Routes not instant after creation | Typically 1-2 minutes; not real-time |
| **IP-based routing discouraged** | Microsoft prefers URL-based filtering | URLs (FQDN) are more stable than IPs, but IPs still necessary for some scenarios |
| **No user-interactive policies** | Cannot route based on user identity | Use security appliance policies instead; this function handles IP routing only |

---

## Testing

The project includes **11 tests** covering both logic validation and real Azure integration.

### Test Suite Overview

| Test Class | Type | What It Tests | Azure Required? |
|---|---|---|---|
| `TestM365API` | Unit | M365 endpoint API client, version fetch, IPv4 extraction | ❌ No (mocked) |
| `TestRouteTableManager` | Unit | Route name generation, sanitization logic | ❌ No (mocked) |
| `TestStateManager` | Unit | State CIDR diff calculation, blob storage logic | ❌ No (mocked) |
| `TestM365LiveIntegration` | Integration | Fetches real M365 IPs (Teams, Exchange, SPO, ODFB) and adds/removes them in Azure | ✅ Yes (real) |
| `TestRouteTableIntegration` | Integration | Real route create/delete in Azure, idempotency, cleanup | ✅ Yes (real) |

### Run Tests

**All tests (unit + integration):**
```bash
python -m pytest tests.py -v
```

**Unit tests only (no Azure required):**
```bash
python -m pytest tests.py::TestM365API tests.py::TestRouteTableManager tests.py::TestStateManager -v
```

**Integration tests only (requires Azure login):**
```bash
python -m pytest tests.py::TestM365LiveIntegration tests.py::TestRouteTableIntegration -v
```

### Integration Tests Details

Integration tests use the real Azure Route Table specified by the `ROUTE_TABLE_NAME` environment variable:

1. **test_add_route_creates_entry_in_azure**
   - Adds route `203.0.113.0/24` (TEST-NET-3, non-routable)
   - Verifies it appears in Azure immediately

2. **test_add_route_is_idempotent**
   - Adds the same route twice
   - Verifies no duplicates or errors on second attempt

3. **test_get_current_routes_returns_list**
   - Queries the real route table
   - Verifies API returns correct structure

4. **test_remove_route_deletes_entry_from_azure**
   - Creates a route, then deletes it
   - Verifies it's gone from Azure

**Cleanup:** Integration tests auto-clean after each test (tearDown removes test routes), so the route table is always in a known state.

### Prerequisites for Integration Tests

- Azure CLI logged in: `az account show`
- Environment variables set: `SUBSCRIPTION_ID`, `RESOURCE_GROUP`, `ROUTE_TABLE_NAME`
- The route table named by `ROUTE_TABLE_NAME` exists in `RESOURCE_GROUP`
- Network Contributor role on the resource group assigned to your identity

### Expected Output

```
============================= test session starts ==============================
platform win32 -- Python 3.11.9, pytest-7.4.3
collected 11 items

tests.py::TestM365API::test_extract_ipv4_cidrs PASSED                             [  9%]
tests.py::TestM365API::test_get_current_version PASSED                            [ 18%]
tests.py::TestRouteTableManager::test_generate_route_name PASSED                  [ 27%]
tests.py::TestStateManager::test_get_diff PASSED                                  [ 36%]
tests.py::TestM365LiveIntegration::test_add_real_m365_routes PASSED               [ 45%]
tests.py::TestM365LiveIntegration::test_add_real_m365_routes_is_idempotent PASSED [ 54%]
tests.py::TestM365LiveIntegration::test_remove_real_m365_routes PASSED            [ 63%]
tests.py::TestRouteTableIntegration::test_add_route_creates_entry_in_azure PASSED [ 72%]
tests.py::TestRouteTableIntegration::test_add_route_is_idempotent PASSED          [ 81%]
tests.py::TestRouteTableIntegration::test_get_current_routes_returns_list PASSED  [ 90%]
tests.py::TestRouteTableIntegration::test_remove_route_deletes_entry_from_azure PASSED [100%]

============================== 11 passed in 45.23s ==============================
```

## Configuration

Set these environment variables on the Function App:

| Variable | Example | Description |
|----------|---------|-------------|
| `SUBSCRIPTION_ID` | `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` | Azure subscription ID |
| `RESOURCE_GROUP` | `my-resource-group` | Resource group containing route tables |
| `ROUTE_TABLE_NAMES` | `route-table-1,route-table-2` | Comma-separated route table names |
| `STORAGE_ACCOUNT_NAME` | `mystorageacct` | Storage account name (without .blob.core.windows.net) |
| `CONTAINER_NAME` | `m365-routes` | Blob container name |
| `NEXT_HOP_TYPE` | `Internet` | `Internet` or `VirtualAppliance` |
| `NEXT_HOP_IP` | `10.0.0.1` | Only required if NEXT_HOP_TYPE is `VirtualAppliance` |

### Example Configuration

```bash
# Set App Settings
az functionapp config appsettings set \
  --resource-group $RESOURCE_GROUP \
  --name $FUNCTION_APP_NAME \
  --settings \
    SUBSCRIPTION_ID=$SUBSCRIPTION_ID \
    RESOURCE_GROUP=$RESOURCE_GROUP \
    ROUTE_TABLE_NAMES="rt-spoke1,rt-spoke2" \
    STORAGE_ACCOUNT_NAME=$STORAGE_ACCOUNT \
    CONTAINER_NAME="m365-routes" \
    NEXT_HOP_TYPE="Internet"
```

## Deployment

### Option 1: Bicep (Recommended)

#### 1. Provision Infrastructure

Edit `infra/main.parameters.json` with your values (see [Configure Deployment Parameters](#quick-start-3-steps) above for parameter descriptions), then:

```bash
# Create resource group if it does not already exist
az group create --name <resource-group> --location <location>

# Deploy all infrastructure
az deployment group create \
  --resource-group <resource-group> \
  --template-file infra/main.bicep \
  --parameters infra/main.parameters.json
```

The Bicep template creates and configures:
- Storage Account and Blob Container (state storage)
- Consumption-plan Linux Function App (Python 3.11) with System-Assigned Managed Identity
- Application Insights
- Network Contributor role on the resource group (route table management)
- Storage Blob Data Contributor role on the storage account (state management)
- All required application settings

#### 2. Deploy Function Code

```bash
# Using Azure Functions Core Tools
func azure functionapp publish <function-app-name> --python

# Alternative: zip deployment
cd function_app && zip -r ../function.zip . && cd ..
az functionapp deployment source config-zip \
  --resource-group <resource-group> \
  --name <function-app-name> \
  --src function.zip
```

#### 3. Verify

```bash
az functionapp show \
  --resource-group <resource-group> \
  --name <function-app-name> \
  --query state

az webapp log tail \
  --resource-group <resource-group> \
  --name <function-app-name>
```

---

### Alternative: Manual Deployment

If you prefer to provision resources step by step with the Azure CLI:

#### 1. Create Azure Resources

```bash
SUBSCRIPTION_ID="..."
RESOURCE_GROUP="udr-automation-rg"
LOCATION="eastus"
FUNCTION_APP_NAME="udr-m365-automation"
STORAGE_ACCOUNT="udram365<unique-suffix>"
CONTAINER_NAME="m365-routes"

# Create resource group
az group create \
  --name $RESOURCE_GROUP \
  --location $LOCATION

# Create storage account
az storage account create \
  --resource-group $RESOURCE_GROUP \
  --name $STORAGE_ACCOUNT \
  --location $LOCATION \
  --sku Standard_LRS

# Create container
az storage container create \
  --account-name $STORAGE_ACCOUNT \
  --name $CONTAINER_NAME

# Create Function App (Python 3.11)
az functionapp create \
  --resource-group $RESOURCE_GROUP \
  --consumption-plan-location $LOCATION \
  --runtime python \
  --runtime-version 3.11 \
  --functions-version 4 \
  --name $FUNCTION_APP_NAME \
  --storage-account $STORAGE_ACCOUNT \
  --assign-identity
```

#### 2. Assign RBAC Roles

Assign the required RBAC roles to the function's Managed Identity. See [Required RBAC Roles](#required-rbac-roles) section above for the specific roles and commands needed.

```bash
az role assignment list \
  --assignee-object-id $FUNCTION_PRINCIPAL_ID \
  --query "[].{Role: roleDefinitionName, Scope: scope}" -o table
```

#### 3. Configure Application Settings

```bash
az functionapp config appsettings set \
  --resource-group $RESOURCE_GROUP \
  --name $FUNCTION_APP_NAME \
  --settings \
    SUBSCRIPTION_ID=$SUBSCRIPTION_ID \
    RESOURCE_GROUP=$RESOURCE_GROUP \
    ROUTE_TABLE_NAMES="your-route-table-name" \
    STORAGE_ACCOUNT_NAME=$STORAGE_ACCOUNT \
    CONTAINER_NAME=$CONTAINER_NAME \
    NEXT_HOP_TYPE="Internet"
```

#### 4. Deploy Function Code

```bash
func azure functionapp publish $FUNCTION_APP_NAME --python

# Alternative: zip deployment
cd function_app && zip -r ../function.zip . && cd ..
az functionapp deployment source config-zip \
  --resource-group $RESOURCE_GROUP \
  --name $FUNCTION_APP_NAME \
  --src function.zip
```

#### 5. Verify

```bash
az functionapp show \
  --resource-group $RESOURCE_GROUP \
  --name $FUNCTION_APP_NAME \
  --query state

az webapp log tail \
  --resource-group $RESOURCE_GROUP \
  --name $FUNCTION_APP_NAME
```

## How It Works

### Flow

1. **Timer Trigger**: Function runs on schedule (default: 0:00 UTC daily)

2. **Fetch M365 Endpoints**:
   - Calls `https://endpoints.office.com/endpoints/worldwide`
   - Filters for "Optimize" and "Allow" categories
   - Extracts IPv4 CIDR blocks (excludes IPv6)

3. **Get Version**:
   - Retrieves current M365 API version
   - Stored for tracking and change detection

4. **Calculate Diff**:
   - Reads previous CIDR list from Blob Storage
   - Compares with new list
   - Determines routes to add and remove

5. **Apply Changes**:
   - **Remove** stale routes (with error handling for limits)
   - **Add** new routes (respects ~400 route limit per table)
   - Logs all operations

6. **Save State**:
   - Updates Blob Storage with new version and CIDR list
   - Includes timestamp for audit trail

### Idempotency

- Routes already present in the table are skipped
- No changes = early exit (no unnecessary operations)
- Diff-based approach prevents duplicate routes

### Limits & Constraints

- **Route limit**: ~400 routes per route table (enforced)
- **Route name format**: `m365_<cidr_with_underscores>`
- **IPv4 only**: IPv6 addresses are filtered out
- **Deduplication**: Automatic based on destination CIDR

## Troubleshooting

### Function fails with authentication error

**Cause**: Managed Identity lacks required RBAC roles

**Solution**:
```bash
# Verify role assignment
az role assignment list \
  --all \
  --assignee $FUNCTION_PRINCIPAL_ID \
  --query "[].{Role: roleDefinitionName, Scope: scope}" -o table

# Grant missing roles (see RBAC section above)
```

### No routes are updated

**Check**:
1. Review function logs: `az webapp log tail --resource-group $RG --name $FUNCTION`
2. Verify environment variables are set correctly
3. Ensure route table name matches exactly
4. Confirm M365 API is reachable: `curl https://endpoints.office.com/version/worldwide`

### Routes reaching limit

**Symptom**: New routes not added, function logs show "at capacity"

**Solution**:
1. Remove obsolete routes manually or create additional route tables
2. Consider filtering categories more strictly (e.g., "Optimize" only)
3. Monitor route count over time and plan capacity

### Storage account access denied

**Cause**: Blob Storage role not assigned to identity

**Solution**:
```bash
az role assignment create \
  --assignee-object-id $FUNCTION_PRINCIPAL_ID \
  --role "Storage Blob Data Contributor" \
  --scope /subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.Storage/storageAccounts/$STORAGE_ACCOUNT_NAME
```

## Monitoring

### Key Metrics to Track

- **Function execution count**: Monitor daily executions
- **Function duration**: Typically < 30 seconds (adjust timeout if needed)
- **Routes added/removed**: Check function logs for summary
- **Errors**: Set up alerts for failures

### Enable Application Insights

```bash
az functionapp config appsettings set \
  --resource-group $RESOURCE_GROUP \
  --name $FUNCTION_APP_NAME \
  --settings APPINSIGHTS_INSTRUMENTATIONKEY=$APPINSIGHTS_KEY
```

### Sample KQL Query (Log Analytics)

```kusto
traces
| where message contains "EXECUTION SUMMARY"
| project timestamp, message
| order by timestamp desc
| limit 20
```

## Security Considerations

1. **Managed Identity**: Uses Azure AD Managed Identity, no storage of credentials
2. **RBAC**: Least-privilege roles assigned (Network Contributor, Storage Blob Data Contributor)
3. **API**: M365 endpoint API is public, no authentication required
4. **State Storage**: Blob stored in Azure Storage with private access (no public endpoint)
5. **Logging**: All changes logged with timestamps for audit

## Cost Estimation

| Service | Usage | Monthly Cost |
|---------|-------|--------------|
| Azure Function | ~30 executions/month, < 1 min each | < $1 |
| Azure Storage (Blob) | < 1 MB state blob, minimal I/O | < $1 |
| **Total** | | **< $2/month** |

## Customization

### Change Schedule

Update the timer schedule in the function definition:

- If using the v1 model, schedule is defined in `function_app/function.json`.
- For Python v2, update the timer trigger decorator in code.

```json
"schedule": "0 0 2 * * *"  // 2:00 AM UTC daily (6-field NCronTab format)
"schedule": "0 0 */6 * * *" // Every 6 hours
```

### Filter Categories

In `function_app/function_app.py`, modify:

```python
endpoints = get_endpoints(categories=["Optimize"])  # Only Optimize
```

### Custom Next Hop

Update environment variable:

```bash
az functionapp config appsettings set \
  --resource-group $RESOURCE_GROUP \
  --name $FUNCTION_APP_NAME \
  --settings \
    NEXT_HOP_TYPE="VirtualAppliance" \
    NEXT_HOP_IP="10.0.0.1"  # Your NVA IP
```

## References

- [Microsoft 365 Endpoint Service](https://learn.microsoft.com/en-us/microsoft-365/enterprise/microsoft-365-ip-web-service)
- [Azure Route Tables Documentation](https://learn.microsoft.com/en-us/azure/virtual-network/manage-route-table)
- [Azure Functions Python Developer Guide](https://learn.microsoft.com/en-us/azure/azure-functions/functions-reference-python)
- [Azure Managed Identity](https://learn.microsoft.com/en-us/azure/active-directory/managed-identities-azure-resources/overview)

## License

MIT

## Support

For issues or questions:
1. Review troubleshooting section
2. Check Azure Function logs
3. Verify M365 API availability: https://learn.microsoft.com/en-us/microsoft-365/enterprise/microsoft-365-ip-web-service

## Future Enhancements

Possible improvements for future versions:

### Near-term
- **Azure Virtual Network Manager (AVNM)** integration
  - Centralized multi-region route updates
  - Automated route table sharding across subscriptions

- **Service Tags support**
  - Azure services (SQL, Storage) could also bypass inspection
  - Reduces manual maintenance for non-M365 services

- **/changes endpoint optimization**
  - Use Microsoft's `/changes` endpoint instead of full sync
  - More efficient API calls; delta updates only

### Long-term
- **IPv6 support**
  - M365 IPv6 endpoint data (when more widely available)
  - Dual-stack VNet support

- **Route summarization / CIDR aggregation**
  - Reduce 2,000+ routes to ~50 via RFC 4632
  - Requires native Azure SDK support (currently RFC 4632 aggregation must be done externally)

- **Conditional routing**
  - Route specific M365 apps (Teams, Exchange only) while others inspect
  - Requires more granular M365 API categories (Microsoft working on this)
