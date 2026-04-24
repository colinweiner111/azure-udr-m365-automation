# Azure UDR M365 Automation

Keeps Azure Route Tables synchronized with Microsoft 365 IP ranges so M365 traffic (Teams, Exchange, SharePoint) bypasses your security appliance and routes directly to the internet — automatically, daily.

**How it works:** An Azure Function fetches the [M365 endpoint API](https://learn.microsoft.com/en-us/microsoft-365/enterprise/microsoft-365-ip-web-service) daily, diffs the results against saved state, and adds/removes UDRs in your route tables. It also detects and restores routes that were manually deleted (drift detection). All runs are logged as JSON blobs in Azure Storage for audit.

> **When NOT to use this:** If your security appliance supports FQDN/URL-based filtering (e.g., Zscaler URL policies), that is the preferred Microsoft approach. Use UDR-based routing only when IP-based routing is required.

---

## Why these routes?

Microsoft classifies M365 network traffic into [three categories](https://learn.microsoft.com/en-us/microsoft-365/enterprise/microsoft-365-network-connectivity-principles#new-office-365-endpoint-categories). This function defaults to `Optimize` + `Allow`:

| Category | What it covers | Route it direct? |
|----------|---------------|-----------------|
| **Optimize** | Teams real-time media, Exchange Online, SharePoint Online — the highest-volume, most latency-sensitive M365 traffic. Microsoft [explicitly requires](https://learn.microsoft.com/en-us/microsoft-365/enterprise/microsoft-365-network-connectivity-principles#avoid-network-hairpins-for-microsoft-365) these IPs bypass proxies and inspection devices entirely. | **Yes** |
| **Allow** | Additional Exchange Online, SharePoint Online, and OneDrive endpoints. Not as latency-sensitive as Optimize, but Microsoft [recommends](https://learn.microsoft.com/en-us/microsoft-365/enterprise/microsoft-365-network-connectivity-principles#optimizing-connectivity-to-microsoft-365-services) avoiding proxy inspection for these too. | **Yes** |
| **Default** | General Microsoft CDN, telemetry, and cloud traffic that goes well beyond core M365 workloads. Can tolerate your security appliance and should stay on the normal path. | No |

The full list of IPs per category is published at [Microsoft 365 URLs and IP address ranges](https://learn.microsoft.com/en-us/microsoft-365/enterprise/urls-and-ip-address-ranges?view=o365-worldwide).

**Why not `Default`?** The `Default` set covers a very broad range of Microsoft IPs — routing all of it directly would effectively gut your security appliance's visibility into a large chunk of outbound traffic. `Optimize` + `Allow` is the surgical option: ~34 routes as of April 2026, covering exactly the M365 workloads Microsoft says need direct paths, while everything else still flows through your stack.

**Why UDRs at all?** In hub-and-spoke networks with a [forced tunnel](https://learn.microsoft.com/en-us/azure/vpn-gateway/vpn-gateway-forced-tunneling-rm) (`0.0.0.0/0 → NVA or VPN gateway`), M365 traffic hairpins through your security appliance before reaching the internet. Microsoft [calls this out](https://learn.microsoft.com/en-us/microsoft-365/enterprise/microsoft-365-network-connectivity-principles#avoid-network-hairpins-for-microsoft-365) as a primary cause of poor Teams call quality and slow SharePoint performance. UDRs with `nextHopType: Internet` on the M365 CIDRs punch precise holes in the forced tunnel — M365 breaks out locally, everything else still goes through the appliance.

---

## Prerequisites

- Azure subscription with permission to create resources and assign RBAC roles
- One or more existing Route Tables in the target resource group (Bicep provisions the first one; additional tables must be pre-created)

---

## Deploy

> **Everything below can be run entirely from [Azure Cloud Shell](https://shell.azure.com)** — no local tooling required. Cloud Shell has the Azure CLI, `pip`, and `git` pre-installed and you're already authenticated.

### 1. Clone the repo and set your subscription

```bash
git clone https://github.com/colinweiner111/azure-udr-m365-automation.git
cd azure-udr-m365-automation
az account set --subscription <subscription-id>
```

### 2. Configure parameters

Open the parameters file in the Cloud Shell editor:

```bash
code infra/main.parameters.json
```

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
  --resource-group <resource-group> \
  --template-file infra/main.bicep \
  --parameters infra/main.parameters.json
```

> Takes under 20 minutes. Azure Cloud Shell disconnects after 20 minutes of inactivity — the deployment completes well within that window.

Bicep creates: Storage Account, Blob containers, Flex Consumption Function App (Python 3.11, FC1) with System-Assigned Managed Identity, Application Insights, and all required RBAC role assignments (Network Contributor on the RG, Storage Blob/Queue/Table Data Contributor on the storage account).

### 4. Deploy function code

Still in the same Cloud Shell session:

```bash
# Build zip (Linux-compiled packages required)
pip install --target .python_packages/lib/site-packages -r requirements.txt --platform manylinux2014_x86_64 --only-binary=:all:
zip -r function.zip . -x "*.git*" "local.settings.json" "__pycache__/*" "*.pyc" "tests.py" "infra/*"

# Deploy zip to Flex Consumption function app
az functionapp deployment source config-zip \
  --resource-group <resource-group> \
  --name <function-app-name> \
  --src function.zip
```

> Takes under a minute.

### 5. Verify

```bash
az functionapp show --resource-group <resource-group> --name <function-app-name> --query state

az webapp log tail --resource-group <resource-group> --name <function-app-name>
```

The function runs automatically at midnight UTC daily (`0 0 0 * * *`).

---

## Trigger manually

**From the CLI:**

```bash
az rest --method post \
  --uri "https://management.azure.com/subscriptions/<subscription-id>/resourceGroups/<resource-group>/providers/Microsoft.Web/sites/<function-app-name>/hostruntime/admin/functions/update_m365_routes/trigger?api-version=2024-04-01"
```

**From the Azure Portal:** Open the Function App → Functions → `update_m365_routes` → Code + Test → Test/Run.

---

## Run logs

Each run writes a JSON blob to the `run-logs` container in your storage account, organized by date (`YYYY/MM/DD/HH-MM-SS.json`). Browse them in Azure Storage Explorer or the portal.

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
- The zip hasn't been deployed yet.
- Run `az functionapp deployment source config-zip` as shown in Step 4.

**Routes were deleted and not restored**
- Drift detection runs on every execution. Trigger manually (see above) to restore immediately rather than waiting for the next daily run.

**Azure Policy wiping route tables**
- Policies with a `Modify` effect can overwrite route table properties. Exempt the resource group from those policies. The function will restore any removed routes on the next run (daily or manual trigger).

---

## References

- [Microsoft 365 IP Web Service](https://learn.microsoft.com/en-us/microsoft-365/enterprise/microsoft-365-ip-web-service)
- [M365 Endpoint Categories (Optimize / Allow / Default)](https://learn.microsoft.com/en-us/microsoft-365/enterprise/microsoft-365-network-connectivity-principles#identify-microsoft-365-network-traffic)
- [M365 Endpoints API — worldwide endpoints](https://endpoints.office.com/endpoints/worldwide?clientrequestid=b10c5ed1-bad1-445f-b386-b919946339a7) *(live JSON the function pulls)*
- [M365 Endpoints API — current version](https://endpoints.office.com/version/worldwide?clientrequestid=b10c5ed1-bad1-445f-b386-b919946339a7) *(version number used in run logs)*
- [Azure Route Tables](https://learn.microsoft.com/en-us/azure/virtual-network/manage-route-table)
- [Azure Functions Python Developer Guide](https://learn.microsoft.com/en-us/azure/azure-functions/functions-reference-python)

## License

MIT
