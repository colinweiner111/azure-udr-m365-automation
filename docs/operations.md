# Operations Reference

---

## Run Log Schema

Each run writes a JSON blob to `run-logs/<service>/YYYY/MM/DD/HH-MM-SS.json` in the storage account (`m365/` for M365 runs, `intune/` for Intune runs). Browse them in Azure Storage Explorer or the portal.

```json
{
  "timestamp": "2026-04-23T01:03:20Z",
  "duration_seconds": 8,
  "result": "success",
  "source_version": "2026033100",
  "total_routes": 34,
  "added": ["52.96.0.0/14"],
  "removed": [],
  "drift_restored": [],
  "add_succeeded": 1,
  "add_failed": 0,
  "remove_succeeded": 0,
  "remove_failed": 0,
  "tables": {
    "rg-spoke1/rt-spoke1": {
      "added": 1,
      "add_failed": 0,
      "added_routes": ["52.96.0.0/14"],
      "failed_routes": [],
      "removed": 0,
      "remove_failed": 0,
      "removed_routes": [],
      "errors": []
    }
  }
}
```

| Field | Description |
|-------|-------------|
| `result` | `success`, `no_change`, or `error` |
| `source_version` | M365 API version string or Intune doc date (`YYYY-MM-DD`) |
| `total_routes` | Total routes in the published CIDR list for this run |
| `added` | CIDRs added this run (new endpoints from the source) |
| `removed` | CIDRs removed this run (dropped from the source) |
| `drift_restored` | CIDRs restored this run (were in the source but missing from the tables) |
| `add_succeeded` / `add_failed` | Route additions across all tables |
| `remove_succeeded` / `remove_failed` | Route removals across all tables |
| `duration_seconds` | Wall-clock time for the full run |
| `tables` | Per-table breakdown with route-level detail and error arrays |

---

## Troubleshooting

**Authentication error / routes not updating**
- Verify RBAC: `az role assignment list --assignee <principal-id> --query "[].{Role:roleDefinitionName, Scope:scope}" -o table`
- The identity needs Network Contributor on the route table RG and Storage Blob Data Contributor on the storage account (Bicep assigns these automatically within the deployment RG).

**`RoleAssignmentUpdateNotPermitted` during Bicep deployment**
- Stale/orphaned role assignment from an older Function App managed identity.
- Delete stale assignments at the deployment RG (Network Contributor) and storage account (Storage Blob/Queue/Table Data Contributor) scopes, then rerun the infrastructure deployment.
- After infrastructure succeeds, run the zip deployment step again.

**Invalid route table name when using `rg/tablename`**
- Pull the latest repo version. Older template behavior could try to create a route table from the raw `routeTableNames` entry, which fails when the value includes `/`.
- If deploying manually, pre-create every route table listed in `routeTableNames` before running Bicep.
- If deployment still fails at role assignments, this is an RBAC permission issue — not a route-table-name issue. Have an Owner or User Access Administrator run the Bicep deployment, or pre-create the required role assignments.

**Managed identity deleted or role assignments missing**
- If the Function App is re-created or its managed identity is deleted (e.g. by an Azure Policy cleanup job), role assignments are orphaned and must be re-applied.
- Re-run the Bicep deployment to create a new identity and re-assign roles within the deployment RG.
- Then re-run the cross-RG assignments from [deployment.md step 3a](deployment.md#3a-assign-network-contributor-on-additional-resource-groups) for any additional resource groups.
- Orphaned assignments show up as `Unknown` principals in IAM and can be safely deleted.

**Function shows ServiceUnavailable after deploy**
- The zip hasn't been deployed yet. Run `az functionapp deployment source config-zip` as shown in [deployment.md step 4](deployment.md#4-deploy-function-code).

**Routes were deleted and not restored**
- Drift detection runs on every execution. Trigger manually (see [Trigger manually](../README.md#trigger-manually)) to restore immediately rather than waiting for the next daily run.

**Azure Policy wiping route tables**
- Policies with a `Modify` effect can overwrite route table properties. Exempt the resource group from those policies. The function will restore removed routes on the next run (daily or manual trigger).

**Will the function remove my custom/non-M365 routes?**
- No. The function only manages routes whose address prefixes appear in its source list (M365 API or Intune published list). Any route added manually with a prefix outside those lists is never modified or removed. The one exception: if a CIDR you added manually happens to match a prefix Microsoft later drops from their published list, the function removes it as part of normal cleanup.

**How does the Intune IP list stay current when Microsoft updates their endpoints?**
- Automatically. Each daily Intune sync checks the commit SHA of `endpoints.md` in the MicrosoftDocs/memdocs GitHub repo against the last-known SHA stored in blob. If it changed, the function fetches the raw file, parses the IPv4 CIDRs, stores the updated list in blob (`m365-routes/doc-version/intune_cidrs.json`), and uses the new list immediately — no redeploy needed.
- Check Application Insights logs after a run for `"Intune CIDR list auto-updated"` with the before/after commit SHAs and the new CIDR count.
- If you see a parse failure warning, Microsoft restructured the page in a way the parser could not handle. In that case, update `shared/intune_api.py` manually with the new list and redeploy to restore the hardcoded fallback.

---

## Customer Onboarding

Use one Function App deployment per customer subscription.

- Keep each customer isolated to one subscription and one Function App.
- Use `ROUTE_TABLE_NAMES` only for route tables in that same subscription.
- For tables in other resource groups, grant the Function App managed identity `Network Contributor` on each resource group.
- Let customers update only operational app settings (`M365_ROUTE_SYNC_SCHEDULE`, `INTUNE_ROUTE_SYNC_SCHEDULE`, `ROUTE_TABLE_NAMES`) in the portal.
- Keep the customer parameters file in source control and update it after any portal-only change so future redeployments stay in sync.

**Customer handoff checklist:**

- [ ] Route tables exist and RBAC is assigned on every route-table resource group
- [ ] Triggered `update_m365_routes` manually; latest `m365/` run-log has no table-level errors
- [ ] Triggered `update_intune_routes` manually (first run is the initial seed — a few minutes); `intune/` run-log shows `result: "success"` and all tables have 0 errors
- [ ] `M365_ROUTE_SYNC_SCHEDULE` and `INTUNE_ROUTE_SYNC_SCHEDULE` set to the agreed UTC schedules
