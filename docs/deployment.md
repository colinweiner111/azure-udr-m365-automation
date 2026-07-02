# Deployment Guide

Full manual deployment steps for azure-udr-m365-automation. For the quick path using `deploy.ps1`, see the [README](../README.md#deploy).

---

## Azure RBAC Requirements

### Your account (deploying the solution)

| Role | Scope | Purpose |
|------|-------|---------|
| **Contributor** (or Owner) | Subscription or Resource Group | Create Function App, Storage Account, and App Service Plan |
| **User Access Administrator** (or Owner) | Subscription or Resource Group | Assign RBAC roles to the managed identity during Bicep deployment |

> Owner at the resource group level satisfies both rows. If your account only has Contributor, a separate Owner or User Access Administrator must run the Bicep deployment or pre-create the role assignments manually.

### Function App managed identity (runtime)

Bicep assigns these automatically within the deployment resource group:

| Role | Scope | Purpose |
|------|-------|---------|
| **Network Contributor** | Resource Group | Read and update Route Tables |
| **Storage Blob Data Contributor** | Storage Account | Read/write route-state blobs, run-log blobs, and the deployment package |
| **Storage Queue Data Contributor** | Storage Account | Functions host storage (Flex Consumption) |
| **Storage Table Data Contributor** | Storage Account | Functions host storage (Flex Consumption) |

For tables in additional resource groups, see [Step 3a](#3a-assign-network-contributor-on-additional-resource-groups).

> The managed identity is tied to the Function App's lifecycle. If you delete and recreate the Function App (e.g. to fix a broken deployment), a new identity is created and cross-RG role assignments must be re-applied. Orphaned assignments from the old identity show up as `Unknown` principals in IAM and can be safely deleted.

---

## Manual Steps

### 1. Clone the repo and set your subscription

```bash
git clone https://github.com/colinweiner111/azure-udr-m365-automation.git
cd azure-udr-m365-automation
az account set --subscription <subscription-id>
```

> **Caution:** The manual path depends on the currently selected Azure CLI context. Unlike `deploy.ps1`, it does not verify that your active subscription matches `subscriptionId` in the parameters file.

> **Caution:** The manual path does not auto-create route tables. Ensure every table listed in `routeTableNames` already exists before running Bicep.

### 2. Configure parameters

```bash
code infra/main.parameters.json
```

For separate environments, use dedicated parameter files:

```bash
code infra/main.testing.parameters.json
code infra/main.prod.parameters.json
```

Use different values per environment for `functionAppName`, `storageAccountName`, `routeTableNames`, and the deployment resource group to keep test and production state isolated.

For customer deployments, copy `infra/main.customer.parameters.template.json` to a customer-specific file and fill in the subscription, region, function app name, storage account name, and route tables.

| Parameter | Description | Required |
|-----------|-------------|----------|
| `subscriptionId` | Azure subscription ID | Yes |
| `functionAppName` | Function App name (becomes `<name>.azurewebsites.net`) — must be globally unique | Yes |
| `storageAccountName` | 3–24 chars, lowercase + numbers, globally unique | Yes |
| `routeTableNames` | Comma-separated route tables. Bare name uses the deployment RG; `rg/tablename` targets another RG. Example: `rg-spoke1/rt-spoke1,rg-spoke2/rt-spoke2` | Yes |
| `location` | Azure region (e.g., `centralus`) | Yes |
| `nextHopType` | `Internet` or `VirtualAppliance` | Default: `Internet` |
| `nextHopIp` | NVA private IP — required when `nextHopType` is `VirtualAppliance` | Conditional |
| `containerName` | Blob container for route state | Default: `m365-routes` |
| `m365Categories` | M365 categories to sync: `Optimize`, `Allow`, `Default` | Default: `Optimize,Allow` |
| `m365RouteSyncSchedule` | Timer schedule in UTC (NCRONTAB, 6 fields: `sec min hour day month day-of-week`) | Default: `0 0 0 * * *` |
| `intuneRouteTableNames` | Route tables for Intune routes. Same format as `routeTableNames`. | Default: same as `routeTableNames` |
| `intuneRouteSyncSchedule` | Timer schedule for Intune sync in UTC (NCRONTAB, 6 fields). Offset from M365 to avoid overlap. | Default: `0 30 0 * * *` |

> **Parameter → app setting mapping:** `routeTableNames` → `ROUTE_TABLE_NAMES`, `containerName` → `CONTAINER_NAME`, `m365Categories` → `M365_CATEGORIES`, `m365RouteSyncSchedule` → `M365_ROUTE_SYNC_SCHEDULE`, `intuneRouteTableNames` → `INTUNE_ROUTE_TABLE_NAMES`, `intuneRouteSyncSchedule` → `INTUNE_ROUTE_SYNC_SCHEDULE`.

> **Route table limit:** Azure caps each route table at 400 routes. M365 `Optimize,Allow` is ~34 routes; Intune adds ~85. Combined ~119 per table — well within the limit.

### 3. Provision infrastructure

```bash
az group create --name <resource-group> --location <location>

az deployment group create \
  --resource-group <resource-group> \
  --template-file infra/main.bicep \
  --parameters infra/main.testing.parameters.json
```

> Takes under 20 minutes. Azure Cloud Shell disconnects after 20 minutes of inactivity — the deployment completes well within that window.

Bicep creates: Storage Account, Blob containers, Flex Consumption Function App (Python 3.11, FC1) with System-Assigned Managed Identity, Application Insights, and all required RBAC role assignments within the deployment resource group.

### 3a. Assign Network Contributor on additional resource groups

Skip this step if all your route tables are in the deployment resource group.

If `routeTableNames` includes tables in other resource groups (`rg/tablename` format), assign Network Contributor on each of those RGs now:

```bash
PRINCIPAL_ID=$(az functionapp show \
  --resource-group <resource-group> \
  --name <function-app-name> \
  --query identity.principalId -o tsv)

for RG in rg-dept01 rg-dept02 rg-dept03; do
  az role assignment create \
    --assignee-object-id $PRINCIPAL_ID \
    --assignee-principal-type ServicePrincipal \
    --role "Network Contributor" \
    --scope "/subscriptions/<subscription-id>/resourceGroups/$RG"
done
```

> Wait at least 5 minutes after assigning roles before triggering the first run or validation. RBAC propagation is not immediate.

> If using `deploy.ps1`, this step is handled automatically.

### 4. Deploy function code

```bash
# Build zip (Linux-compiled packages required for Flex Consumption)
pip install --target .python_packages/lib/site-packages -r requirements.txt \
  --platform manylinux2014_x86_64 --only-binary=:all:

zip -r function.zip . -x "*.git*" "local.settings.json" "__pycache__/*" "*.pyc" "tests.py" "infra/*"

# Deploy
az functionapp deployment source config-zip \
  --resource-group <resource-group> \
  --name <function-app-name> \
  --src function.zip
```

> **Cloud Shell note:** You may see pip dependency-resolver warnings. If the install command succeeds and `function.zip` is created, those warnings are non-blocking.

### 5. Verify

```bash
az functionapp show --resource-group <resource-group> --name <function-app-name> --query state

az webapp log tail --resource-group <resource-group> --name <function-app-name>
```

Verify role assignments:

```bash
PRINCIPAL_ID=$(az functionapp show \
  --resource-group <resource-group> \
  --name <function-app-name> \
  --query identity.principalId -o tsv)

az role assignment list \
  --assignee $PRINCIPAL_ID \
  --query "[].{Role:roleDefinitionName, Scope:scope}" \
  -o table
```

Trigger one manual run after deployment and inspect the newest blob in the `run-logs` container. The deployment is not complete until every table under `tables` shows an empty `errors` array.
