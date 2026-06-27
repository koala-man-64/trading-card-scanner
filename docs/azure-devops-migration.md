# Azure DevOps Migration

`trading-card-scanner` is migrating to Azure Repos, Azure Pipelines, and Azure Boards in the `rdprokes/AdaptiveAssetAllocation` project.

## Source Control

- Azure Repo: `https://dev.azure.com/rdprokes/AdaptiveAssetAllocation/_git/trading-card-scanner`
- Default branch: `main`
- GitHub remains active until Azure quality, security, release, deploy, and smoke checks prove parity.
- Do not disable `.github/workflows/*` until the Azure `deploy-npe` evidence shows the merged source SHA is deployed to `fa-trading-card-scanner-npe`.

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
- Variable group: `vg-trading-card-scanner-npe`
- Required variable: `FUNCTION_APP_RESOURCE_GROUP=fa-trading-card-scanner-npe_group`
- Optional variable: `FUNCTION_APP_BASE_URL=https://fa-trading-card-scanner-npe-evcpctgjhhcthjgq.eastus2-01.azurewebsites.net`
- Environment: `trading-card-scanner-npe`

Do not store publish profiles, client secrets, storage keys, Function keys, `.env`, or `local.settings.json` in the repo or pipeline YAML.

## Branch Policies

After the first successful manual quality and security runs, protect `main` with:

- Required work item linking
- Minimum one reviewer
- Build validation for `trading-card-scanner-quality`
- Build validation for `trading-card-scanner-security`
- No direct pushes

## Cutover Criteria

GitHub Actions can be disabled only after all checks are true:

- Azure Repo default branch is `refs/heads/main`.
- Azure PR validation passes on quality and security.
- Release publishes `scanner-release` with `release.zip` and `release-manifest.json`.
- Deploy NPE publishes `scanner-deploy-evidence`.
- Function App tags include matching source SHA, release run, deploy run, and artifact SHA.
- `/api/health` and `/api/ready` smoke checks pass.
