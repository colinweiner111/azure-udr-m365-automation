@description('Azure subscription ID; defaults to the deployment subscription.')
param subscriptionId string = subscription().subscriptionId

@description('Resource group name; defaults to the deployment resource group.')
param resourceGroupName string = resourceGroup().name

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Name of the Azure Function App.')
param functionAppName string

@description('Comma-separated list of Azure Route Table names to manage.')
param routeTableNames string

@description('Next hop type for M365 routes.')
@allowed(['Internet', 'VirtualAppliance'])
param nextHopType string = 'Internet'

@description('Next hop IP address; required when nextHopType is VirtualAppliance.')
param nextHopIp string = ''

@description('Globally unique name for the Azure Storage Account.')
param storageAccountName string

@description('Blob container name for M365 route state.')
param containerName string = 'm365-routes'

// Route Table for M365 UDRs
resource routeTable 'Microsoft.Network/routeTables@2023-09-01' = {
  name: split(routeTableNames, ',')[0]
  location: location
  properties: {
    disableBgpRoutePropagation: false
  }
}

// Application Insights
resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${functionAppName}-insights'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    RetentionInDays: 30
  }
}

// Storage Account
// Note: allowSharedKeyAccess is required for Linux Consumption plan. The WEBSITE_CONTENTAZUREFILECONNECTIONSTRING
// app setting mounts an Azure Files share via SMB for the function runtime filesystem, and Azure Files SMB
// does not support managed identity auth. This is a platform limitation; see https://aka.ms/functions-storage-managed-identity.
resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
    allowSharedKeyAccess: true // Required: Azure Files SMB (used by consumption plan) does not support managed identity
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storageAccount
  name: 'default'
}

resource stateContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: containerName
  properties: {
    publicAccess: 'None'
  }
}

// Consumption plan (Linux)
resource hostingPlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: '${functionAppName}-plan'
  location: location
  kind: 'functionapp'
  sku: {
    name: 'Y1'
    tier: 'Dynamic'
  }
  properties: {
    reserved: true // required for Linux
  }
}

// Account key connection string — scoped only to WEBSITE_CONTENTAZUREFILECONNECTIONSTRING (Azure Files mount).
// AzureWebJobsStorage uses managed identity instead (see RBAC assignments below).
var storageFileConnectionString = 'DefaultEndpointsProtocol=https;AccountName=${storageAccount.name};EndpointSuffix=${environment().suffixes.storage};AccountKey=${storageAccount.listKeys().keys[0].value}'

// Function App with System-Assigned Managed Identity
resource functionApp 'Microsoft.Web/sites@2023-12-01' = {
  name: functionAppName
  location: location
  kind: 'functionapp,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: hostingPlan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'Python|3.11'
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      // Enable SCM basic auth so Kudu zip deploy works during CI/CD
      scmIpSecurityRestrictionsUseMain: false
      appSettings: [
        // AzureWebJobsStorage uses managed identity (blob/queue/table — no account key)
        { name: 'AzureWebJobsStorage__accountName', value: storageAccount.name }
        { name: 'AzureWebJobsStorage__blobServiceUri', value: 'https://${storageAccount.name}.blob.${environment().suffixes.storage}' }
        { name: 'AzureWebJobsStorage__queueServiceUri', value: 'https://${storageAccount.name}.queue.${environment().suffixes.storage}' }
        { name: 'AzureWebJobsStorage__tableServiceUri', value: 'https://${storageAccount.name}.table.${environment().suffixes.storage}' }
        { name: 'AzureWebJobsStorage__credential', value: 'managedidentity' }
        // Azure Files content share requires account key (no managed identity support for SMB on consumption plan)
        { name: 'WEBSITE_CONTENTAZUREFILECONNECTIONSTRING', value: storageFileConnectionString }
        { name: 'WEBSITE_CONTENTSHARE', value: toLower(functionAppName) }
        { name: 'FUNCTIONS_EXTENSION_VERSION', value: '~4' }
        { name: 'FUNCTIONS_WORKER_RUNTIME', value: 'python' }
        // Oryx builds packages on deployment (pip install from requirements.txt)
        { name: 'SCM_DO_BUILD_DURING_DEPLOYMENT', value: 'true' }
        { name: 'ENABLE_ORYX_BUILD', value: 'true' }
        { name: 'APPINSIGHTS_INSTRUMENTATIONKEY', value: appInsights.properties.InstrumentationKey }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
        { name: 'SUBSCRIPTION_ID', value: subscriptionId }
        { name: 'RESOURCE_GROUP', value: resourceGroupName }
        { name: 'ROUTE_TABLE_NAMES', value: routeTableNames }
        { name: 'STORAGE_ACCOUNT_NAME', value: storageAccountName }
        { name: 'CONTAINER_NAME', value: containerName }
        { name: 'NEXT_HOP_TYPE', value: nextHopType }
        { name: 'NEXT_HOP_IP', value: nextHopIp }
      ]
    }
  }
}

// Allow SCM (Kudu) basic auth for zip deploy during CI/CD
resource scmBasicAuth 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2023-12-01' = {
  parent: functionApp
  name: 'scm'
  properties: {
    allow: true
  }
}

var networkContributorRoleId = '4d97b98b-1d4f-4787-a291-c67834d212e7'
// Storage roles for managed identity AzureWebJobsStorage (host runtime needs blob + queue + table)
var storageBlobDataOwnerRoleId = 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
var storageQueueDataContributorRoleId = '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
var storageTableDataContributorRoleId = '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'

// Network Contributor on the resource group (for route table management)
resource networkContributorAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, functionApp.id, networkContributorRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', networkContributorRoleId)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Storage Blob Data Owner (blob state management + AzureWebJobsStorage host runtime)
resource storageBlobRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, functionApp.id, storageBlobDataOwnerRoleId)
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataOwnerRoleId)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Storage Queue Data Contributor (required by Functions host runtime for triggers and locks)
resource storageQueueRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, functionApp.id, storageQueueDataContributorRoleId)
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageQueueDataContributorRoleId)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Storage Table Data Contributor (required by Functions host runtime for state)
resource storageTableRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, functionApp.id, storageTableDataContributorRoleId)
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageTableDataContributorRoleId)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

output functionAppName string = functionApp.name
output principalId string = functionApp.identity.principalId
output storageAccountName string = storageAccount.name
output appInsightsConnectionString string = appInsights.properties.ConnectionString
