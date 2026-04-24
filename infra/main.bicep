@description('Azure subscription ID; defaults to the deployment subscription.')
param subscriptionId string = subscription().subscriptionId

@description('Resource group name; defaults to the deployment resource group.')
param resourceGroupName string = resourceGroup().name

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Name of the Azure Function App.')
param functionAppName string

@description('Comma-separated list of Azure Route Table names to manage. Note: Bicep provisions only the first table in this list. Any additional tables must be pre-created before deployment.')
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

@description('Comma-separated M365 endpoint categories to include in route tables (Optimize, Allow, Default).')
param m365Categories string = 'Optimize,Allow'

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
    allowSharedKeyAccess: false // Flex Consumption uses managed identity for all storage access; no Azure Files mount required
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

resource runLogsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'run-logs'
  properties: {
    publicAccess: 'None'
  }
}

resource packageContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'scm-releases'
  properties: {
    publicAccess: 'None'
  }
}

// Flex Consumption plan (Linux)
resource hostingPlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: '${functionAppName}-plan'
  location: location
  kind: 'functionapp'
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  properties: {
    reserved: true
  }
}



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
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: 'https://${storageAccount.name}.blob.${environment().suffixes.storage}/scm-releases'
          authentication: {
            type: 'SystemAssignedIdentity'
          }
        }
      }
      scaleAndConcurrency: {
        instanceMemoryMB: 2048
        maximumInstanceCount: 40
      }
      runtime: {
        name: 'python'
        version: '3.11'
      }
    }
    siteConfig: {
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      cors: {
        allowedOrigins: ['https://portal.azure.com']
        supportCredentials: false
      }
      appSettings: [
        // AzureWebJobsStorage uses managed identity (blob/queue/table — no account key)
        { name: 'AzureWebJobsStorage__accountName', value: storageAccount.name }
        { name: 'AzureWebJobsStorage__blobServiceUri', value: 'https://${storageAccount.name}.blob.${environment().suffixes.storage}' }
        { name: 'AzureWebJobsStorage__queueServiceUri', value: 'https://${storageAccount.name}.queue.${environment().suffixes.storage}' }
        { name: 'AzureWebJobsStorage__tableServiceUri', value: 'https://${storageAccount.name}.table.${environment().suffixes.storage}' }
        { name: 'AzureWebJobsStorage__credential', value: 'managedidentity' }
        // FUNCTIONS_EXTENSION_VERSION and FUNCTIONS_WORKER_RUNTIME are not valid app settings on Flex Consumption
        // — runtime and version are configured via functionAppConfig.runtime above
        { name: 'APPINSIGHTS_INSTRUMENTATIONKEY', value: appInsights.properties.InstrumentationKey }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
        { name: 'SUBSCRIPTION_ID', value: subscriptionId }
        { name: 'RESOURCE_GROUP', value: resourceGroupName }
        { name: 'ROUTE_TABLE_NAMES', value: routeTableNames }
        { name: 'STORAGE_ACCOUNT_NAME', value: storageAccountName }
        { name: 'CONTAINER_NAME', value: containerName }
        { name: 'NEXT_HOP_TYPE', value: nextHopType }
        { name: 'NEXT_HOP_IP', value: nextHopIp }
        // Optional: set a deployment-specific UUID to avoid rate-limit collisions on the M365 endpoints API
        { name: 'M365_CLIENT_REQUEST_ID', value: '' }
        { name: 'M365_CATEGORIES', value: m365Categories }
      ]
    }
  }
}

var networkContributorRoleId = '4d97b98b-1d4f-4787-a291-c67834d212e7'
// Storage roles for managed identity AzureWebJobsStorage (host runtime needs blob + queue + table)
// Storage Blob Data Contributor is sufficient for timer-triggered functions;
// Owner is only required for blob-triggered functions (lease management).
var storageBlobDataContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var storageQueueDataContributorRoleId = '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
var storageTableDataContributorRoleId = '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'

// Network Contributor on the resource group (for route table management)
resource networkContributorAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, functionApp.id, networkContributorRoleId)
  scope: resourceGroup()
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', networkContributorRoleId)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Storage Blob Data Contributor (blob state management + AzureWebJobsStorage host runtime)
resource storageBlobRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, functionApp.id, storageBlobDataContributorRoleId)
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataContributorRoleId)
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
