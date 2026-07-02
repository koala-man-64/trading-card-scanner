# Azure DevOps Migration

`trading-card-scanner` uses Azure Repos, Azure Pipelines, and Azure Boards in the `rdprokes/AdaptiveAssetAllocation` project.

## Source Control

- Azure Repo: `https://dev.azure.com/rdprokes/AdaptiveAssetAllocation/_git/trading-card-scanner`
- Default branch: `main`
- Azure Repos is canonical.
- GitHub should be kept only as a read-only mirror or archived copy.
- GitHub Actions workflow files were removed after Azure quality, security, release, deploy, and smoke checks proved parity.

## Pipelines

- `trading-card-scanner-quality` uses `azure-pipelines/quality.yml`.
- `trading-card-scanner-security` uses `azure-pipelines/security.yml`.
- `trading-card-scanner-release` uses `azure-pipelines/release.yml`.
- `trading-card-scanner-deploy-npe` uses `azure-pipelines/deploy-npe.yml`.

Quality and security run on PRs and `main`. Release runs after the main quality pipeline. Deploy NPE runs from the release artifact and records deployment provenance.

## Azure DevOps Setup

These Azure DevOps resources back the NPE deployment:

- Service connection: `sc-trading-card-scanner-npe-deploy`
- Service connection type: Azure Resource Manager with workload identity federation
- Service connection ID: `cc978729-1c49-4400-825b-a9c8a82a216b`
- Azure RBAC scope: `/subscriptions/eabd0bb1-8f36-4f27-ad86-8b33e02aaeb9/resourceGroups/fa-trading-card-scanner-npe_group`
- Additional required deployment access: read the storage account
  `tcsstorageeastus2` in resource group `tcs-rg` and create/update Event Grid
  event subscriptions on that storage account for the `ProcessBlob` trigger.
- Variable group: `vg-trading-card-scanner-npe`
- Required variable: `FUNCTION_APP_RESOURCE_GROUP=fa-trading-card-scanner-npe_group`
- Optional variable: `FUNCTION_APP_BASE_URL=https://fa-trading-card-scanner-npe-evcpctgjhhcthjgq.eastus2-01.azurewebsites.net`
- Environment: `trading-card-scanner-npe`

Do not store publish profiles, client secrets, storage keys, Function keys, `.env`, or `local.settings.json` in the repo or pipeline YAML.

## Branch Policies

Protect `main` with:

- Required work item linking
- Minimum one reviewer
- Build validation for `trading-card-scanner-quality`
- Build validation for `trading-card-scanner-security`
- No direct pushes

## Cutover Record

GitHub Actions were disabled in the repository after these Azure checks passed:

- Azure Repo default branch is `refs/heads/main`.
- Azure PR validation passes on quality and security.
- Release publishes `scanner-release` with `release.zip` and `release-manifest.json`.
- Deploy NPE publishes `scanner-deploy-evidence`.
- Function App tags include matching source SHA, release run, deploy run, and artifact SHA.
- Keyed `/api/health` and `/api/ready` smoke checks pass.

The first proven Azure deployment used:

- Source SHA: `81b3d6f91dfa0b215c40acaf95843df396558623`
- Quality run: `9645`
- Security run: `9644`
- Release run: `9646`
- Deploy run: `9648`
- Function App: `fa-trading-card-scanner-npe`
