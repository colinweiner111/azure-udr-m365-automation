# Azure UDR M365 Automation

Keeps Azure Route Tables synchronized with Microsoft 365 IP ranges so M365 traffic (Teams, Exchange, SharePoint) bypasses your security appliance and routes directly to the internet — automatically, daily.

**How it works:** An Azure Function fetches the [M365 endpoint API](https://learn.microsoft.com/en-us/microsoft-365/enterprise/microsoft-365-ip-web-service) daily, diffs the results against saved state, and adds/removes UDRs in your route tables. It also detects and restores routes that were manually deleted (drift detection). All runs are logged as JSON blobs in Azure Storage for audit.

> **When NOT to use this:** If your security appliance supports FQDN/URL-based filtering (e.g., Zscaler URL policies), that is the preferred Microsoft approach. Use UDR-based routing only when IP-based routing is required.

---

## Prerequisites

- Azure CLI (`az`) and Azure Functions Core Tools (`func`) installed
- Python 3.11+
- Azure subscription with permission to create resources and assign RBAC roles
- One or more existing Route Tables in the target resource group (Bicep provisions the first one; additional tables must be pre-created)

---

## Deploy

### 1. Log in and set your subscription

```bash
az login
az account set --subscription <subscription-id>
az account show --query "{name:name, id:id}" -o table
```

### 2. Configure parameters

Edit `infra/main.parameters.json`:

| Parameter | Description | Required |
|-----------|-------------|----------|
| `subscriptionId` | Azure subscription ID | Yes |
| `functionAppName` | Function App name (becomes `<name>.azurewebsites.net`) — must be globally unique | Yes |
| `storageAccountName` | Storage account name (3–24 chars, lowercase + numbers, globally unique) | Yes |
| `routeTableNames` | Comma-separated Route Table names to manage (`rt-spoke1,rt-spoke2`) | Yes |
| `location` | Azure region (e.g., `centralus`) | Yes |
| `nextHopType` | `Internet` or `VirtualAppliance` | Default: `Internet` |
| `nextHopIp` | NVA private IP — required only when `nextHopType` is `VirtualAppliance` | Conditional |
| `containerName` | Blob container for route state | Default: `m365-routes` |
| `m365Categories` | M365 categories to include: `Optimize`, `Allow`, `Default` | Default: `Optimize,Allow` |

> **Route table limit:** Azure caps each route table at ~400 routes. `Optimize,Allow` produces ~34 routes as of April 2026 — well within limits.

### 3. Provision infrastructure

```bash
az group create --name <resource-group> --location <location>

az deployment group create \
  --subscription <subscription-id> \
  --resource-group <resource-group> \
  --template-file infra/main.bicep \
  --parameters infra/main.parameters.json
```

Bicep creates: Storage Account, Blob containers, Consumption-plan Linux Function App (Python 3.11) with System-Assigned Managed Identity, Application Insights, and all required RBAC role assignments (Network Contributor on the RG, Storage Blob/Queue/Table Data Contributor on the storage account).

### 4. Deploy function code

Package and upload the function code. The `WEBSITE_RUN_FROM_PACKAGE` app setting points to a blob in the `scm-releases` container — upload your zip there:

```bash
# Build zip (Linux-compiled packages required for Azure Linux consumption plan)
pip install --target .python_packages/lib/site-packages -r requirements.txt --platform manylinux2014_x86_64 --only-binary=:all:
zip -r function.zip . -x "*.git*" "local.settings.json" "__pycache__/*" "*.pyc" "tests.py" "infra/*"

# Upload to storage
CONN=$(az storage account show-connection-string \
  --resource-group <resource-group> \
  --name <storage-account-name> \
  --query connectionString -o tsv)

az storage blob upload \
  --connection-string "$CONN" \
  --container-name scm-releases \
  --name scm-latest-<function-app-name>.zip \
  --file function.zip \
  --overwrite
```

### 5. Verify

```bash
# Check function app is running
az functionapp show --resource-group <resource-group> --name <function-app-name> --query state

# Stream live logs
az webapp log tail --resource-group <resource-group> --name <function-app-name>
```

The function runs automatically at midnight UTC daily (`0 0 0 * * *`).

---

## Trigger manually

**From the CLI** (using the HTTP trigger):

```bash
FUNCTION_KEY=$(az functionapp keys list \
  --resource-group <resource-group> \
  --name <function-app-name> \
  --query "functionKeys.default" -o tsv)

curl -X POST "https://<function-app-name>.azurewebsites.net/api/run?code=$FUNCTION_KEY"
```

**From the Azure Portal:** Open the Function App → Functions → `run_http` → Code + Test → Test/Run.

---

## Run logs

Each run writes a JSON blob to the `run-logs` container in your storage account, organized by date (`YYYY/MM/DD/HH-MM-SS.json`). Browse them in Azure Storage Explorer or the portal.

Example log entry:
```json
{
  "timestamp": "2026-04-23T01:03:20Z",
  "result": "success",
  "m365_version": "2026033100",
  "total_routes": 34,
  "routes_added": ["52.96.0.0/14"],
  "new_from_m365": [],
  "drift_restored": ["52.96.0.0/14"],
  "routes_removed": [],
  "add_succeeded": 1,
  "add_failed": 0
}
```

---

## Troubleshooting

**Authentication error / routes not updating**
- Verify RBAC: `az role assignment list --assignee <principal-id> --query "[].{Role:roleDefinitionName, Scope:scope}" -o table`
- The function identity needs Network Contributor on the RG and Storage Blob Data Contributor on the storage account (Bicep assigns these automatically).

**Function shows ServiceUnavailable after deploy**
- The zip hasn't been uploaded yet, or `WEBSITE_RUN_FROM_PACKAGE` points to a blob that doesn't exist.
- Upload the zip to `scm-releases` as shown in Step 4.

**Routes were deleted and not restored**
- Drift detection runs on every execution. Trigger manually (see above) to restore immediately rather than waiting for the next daily run.

**Azure Policy wiping route tables**
- Policies with a `Modify` effect can overwrite route table properties. Exempt the resource group from those policies. The function will restore any removed routes on the next run (daily or manual trigger).

---

## Future enhancements

- **/changes endpoint**: Use Microsoft's delta endpoint instead of full sync for more efficient API calls
- **Azure Virtual Network Manager**: Centralized multi-region route management
- **IPv6 support**: M365 IPv6 endpoints when more widely adopted
- **CIDR aggregation**: Reduce route count via RFC 4632 summarization (requires external tooling today)

---

## References

- [Microsoft 365 IP Web Service](https://learn.microsoft.com/en-us/microsoft-365/enterprise/microsoft-365-ip-web-service)
- [Azure Route Tables](https://learn.microsoft.com/en-us/azure/virtual-network/manage-route-table)
- [Azure Functions Python Developer Guide](https://learn.microsoft.com/en-us/azure/azure-functions/functions-reference-python)

## License

MIT
