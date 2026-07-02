# Upgrading from M365-only to M365 + Intune

If your Function App already runs `update_m365_routes`, adding Intune sync is an in-place upgrade — no new infrastructure required. The existing M365 function, state, and run logs are unaffected.

---

## Before you start

Verify the managed identity has **Network Contributor** on every resource group containing an Intune target route table. If your Intune route tables are in different resource groups than your M365 ones, add the role assignment now:

```bash
PRINCIPAL_ID=$(az functionapp show \
  --resource-group <resource-group> \
  --name <function-app-name> \
  --query identity.principalId -o tsv)

az role assignment create \
  --assignee-object-id $PRINCIPAL_ID \
  --assignee-principal-type ServicePrincipal \
  --role "Network Contributor" \
  --scope "/subscriptions/<subscription-id>/resourceGroups/<intune-route-table-rg>"
```

> Wait at least 5 minutes after assigning the role before triggering the first Intune seed run.

---

## Safe upgrade sequence

### 1. Deploy updated code

```bash
pip install --target .python_packages/lib/site-packages -r requirements.txt \
  --platform manylinux2014_x86_64 --only-binary=:all:

zip -r function.zip . -x "*.git*" "local.settings.json" "__pycache__/*" "*.pyc" "tests.py" "infra/*"

az functionapp deployment source config-zip \
  --resource-group <resource-group> \
  --name <function-app-name> \
  --src function.zip
```

### 2. Verify both functions appear

In the Azure Portal, open the Function App and confirm both `update_m365_routes` and `update_intune_routes` are listed under Functions. The M365 function continues running on its existing schedule without interruption.

### 3. Add Intune app settings

Function App → Settings → Environment variables → add:

| Setting | Value |
|---------|-------|
| `INTUNE_ROUTE_TABLE_NAMES` | Comma-separated route tables for Intune. Same `rg/tablename` format as `ROUTE_TABLE_NAMES`. Can be the same tables or different ones. |
| `INTUNE_ROUTE_SYNC_SCHEDULE` | NCRONTAB schedule (6 fields). Recommend `0 30 0 * * *` (12:30 AM UTC) to avoid overlap with M365. |

Save and restart the Function App after adding the settings.

### 4. Run the first Intune seed manually

Trigger `update_intune_routes` from the portal or CLI (see [Trigger manually](../README.md#trigger-manually)). The first run seeds all Intune routes (~85 per table). The first run can take a few minutes depending on route table count; subsequent no-change runs usually complete in seconds.

### 5. Confirm success

Check the newest `intune/` blob in the `run-logs` container and verify:

- `result` is `success`
- `add_succeeded` equals `total_routes × number_of_tables`
- `add_failed` is `0`
- every table under `tables` has an empty `errors` array

The upgrade is complete once all tables confirm clean.
