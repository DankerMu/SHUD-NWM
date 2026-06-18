# model-asset-navigation-rbac Specification

## Purpose
TBD - created by archiving change m14-model-asset-management-ui. Update Purpose after archive.
## Requirements
### Requirement: Model asset navigation RBAC
The app SHALL expose system/model asset navigation only to accepted admin roles and render stable denied states for others.

#### Scenario: Admin access
WHEN role is model_admin or sys_admin
THEN the user can open `/system/model-assets` and see the model asset shell

#### Scenario: Viewer denied
WHEN role is viewer/operator
THEN the route is denied or hidden without loading sensitive model detail

#### Scenario: Legacy role term
WHEN design references `version_admin`
THEN the implementation maps access to `model_admin`/`sys_admin` or documents the term as unsupported without adding an untested role

