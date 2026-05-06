# vm-snapshot-policy

**Aria Automation ABX Action — Enterprise On-Demand Snapshot Policy Enforcement**

Enforces environment-based snapshot governance for Day 2 on-demand snapshot requests in VMware Aria Automation. Validates policy compliance, enforces retention limits, and blocks unauthorized snapshot activity in production environments.

---

## Policy Matrix

| Environment | Max Snapshots | Retention | Change Window Required |
|---|---|---|---|
| `PROD` | 1 | 24 hours | ✅ ServiceNow CHG required |
| `UAT` | 1 | 24 hours | ❌ |
| `STG` | 1 | 24 hours | ❌ |
| `DEV` | 3 | 72 hours | ❌ |
| `TEST` | 3 | 72 hours | ❌ |
| `DR` | **BLOCKED** | — | Snapshots break replication chains |

> **Note:** DR environments use vSphere Replication / SRM for data protection. Snapshots are never permitted in DR.

---

## Snapshot Naming Convention

```
SNAP-{VM_NAME}-{ENVIRONMENT}-{YYYYMMDD-HHMMSS}
```

Example: `SNAP-WEB-SERVER-01-PROD-20260505-143022`

---

## Request Types

| Type | Description |
|---|---|
| `CREATE` | Validate policy and approve snapshot creation |
| `DELETE` | Locate and approve snapshot deletion |
| `VALIDATE` | Dry-run policy check — no snapshot created |

---

## Inputs / Outputs

**Inputs (from Aria blueprint):**

| Key | Type | Required | Description |
|---|---|---|---|
| `vmName` | string | ✅ | Target VM name |
| `environment` | string | ✅ | Environment code |
| `requestType` | string | ✅ | CREATE / DELETE / VALIDATE |
| `changeWindowId` | string | PROD only | ServiceNow CHG number (e.g. CHG0012345) |
| `snapshotName` | string | ❌ | Descriptive label for the snapshot |
| `existingSnapshots` | list | ❌ | Existing snapshots for limit enforcement |

**Outputs:**

| Key | Type | Description |
|---|---|---|
| `status` | string | `approved` / `blocked` / `deleted` / `validated` |
| `vmName` | string | Target VM name |
| `snapshotName` | string | Full formatted snapshot name |
| `environment` | string | Validated environment |
| `policy` | dict | Applied policy details |
| `message` | string | Human-readable result summary |

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `SNOW_CHANGE_REQUIRED` | `true` | Set to `false` to disable ServiceNow validation (lab/dev only) |

---

## Running Tests

```bash
pip install pytest
pytest tests/test_snapshot_policy.py -v
```

---

## Author

**Randolph Barden** — [@FantasmaV](https://github.com/FantasmaV)

Senior VCF / Aria Automation Engineer | VMware by Broadcom
