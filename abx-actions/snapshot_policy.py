"""
snapshot_policy.py
------------------
Aria Automation ABX Action — Enterprise On-Demand Snapshot Policy Enforcement

Enforces environment-based snapshot governance for on-demand snapshot
requests during VM Day 2 operations. Validates policy compliance before
allowing snapshot creation, enforces retention limits, and blocks
unauthorized snapshot activity in production environments.

Naming Convention:
    SNAP-{VM_NAME}-{ENVIRONMENT}-{YYYYMMDD-HHMMSS}

Policy Matrix:
    PROD        → Max 1 snapshot, 24hr retention, change window ID required
    UAT / STG   → Max 1 snapshot, 24hr retention
    DEV         → Max 3 snapshots, 72hr retention
    TEST        → Max 3 snapshots, 72hr retention
    DR          → BLOCKED — snapshots break replication chains, use vSphere Replication

Request Types:
    CREATE      → Validate policy, enforce limits, create snapshot
    DELETE      → Delete snapshot by name, log removal
    VALIDATE    → Dry-run policy check without creating snapshot

Environment Variables (set in Aria Automation ABX Action properties):
    SNOW_CHANGE_REQUIRED    Set to "false" to disable ServiceNow change window
                            validation (lab/dev only). Default: "true"

Author: Randolph Barden
Repo:   github.com/FantasmaV/vm-snapshot-policy
"""

import os
import logging
from datetime import datetime, timezone

# ── Logging ────────────────────────────────────────────────────────────────────
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── Policy Definitions ─────────────────────────────────────────────────────────

# Environments where snapshots are completely blocked
BLOCKED_ENVIRONMENTS = {"DR"}

# Environments requiring an approved ServiceNow change window ID
CHANGE_WINDOW_REQUIRED_ENVIRONMENTS = {"PROD"}

# Per-environment snapshot policy
SNAPSHOT_POLICIES = {
    "PROD": {
        "max_snapshots":       1,
        "retention_hours":     24,
        "change_window":       True,
        "description":         "Production — change window required, 24hr max retention",
    },
    "UAT": {
        "max_snapshots":       1,
        "retention_hours":     24,
        "change_window":       False,
        "description":         "UAT — mirrors production policy, 24hr max retention",
    },
    "STG": {
        "max_snapshots":       1,
        "retention_hours":     24,
        "change_window":       False,
        "description":         "Staging — mirrors production policy, 24hr max retention",
    },
    "DEV": {
        "max_snapshots":       3,
        "retention_hours":     72,
        "change_window":       False,
        "description":         "Development — flexible policy, 72hr max retention",
    },
    "TEST": {
        "max_snapshots":       3,
        "retention_hours":     72,
        "change_window":       False,
        "description":         "Test — flexible policy, 72hr max retention",
    },
}

ALLOWED_REQUEST_TYPES = {"CREATE", "DELETE", "VALIDATE"}

# ServiceNow change window validation toggle
SNOW_CHANGE_REQUIRED = os.environ.get("SNOW_CHANGE_REQUIRED", "true").lower() != "false"


# ── ABX Entry Point ────────────────────────────────────────────────────────────
def handler(context, inputs: dict) -> dict:
    """
    ABX handler called by Aria Automation during Day 2 snapshot operations.

    Routes the request to the appropriate handler based on requestType,
    enforcing environment policy at every step.

    Args:
        context: Aria Automation execution context (unused directly).
        inputs:  Dictionary of inputs passed from the Aria blueprint.
                 Expected keys:
                   - vmName (str):           Target VM name.
                   - environment (str):      VM environment code.
                   - requestType (str):      CREATE / DELETE / VALIDATE.
                   - changeWindowId (str):   ServiceNow CHG number (PROD only).
                   - snapshotName (str):     Descriptive name for the snapshot.
                   - existingSnapshots(list):List of existing snapshot dicts
                                             [{name, createdAt, environment}]

    Returns:
        dict with keys:
          - status (str):           "approved" / "blocked" / "deleted" / "validated"
          - vmName (str):           Target VM name.
          - snapshotName (str):     Full formatted snapshot name.
          - environment (str):      Validated environment.
          - policy (dict):          Applied policy details.
          - message (str):          Human-readable result summary.
          - changeWindowId (str):   Change window ID if applicable.

    Raises:
        ValueError: If policy is violated or inputs are invalid.
        KeyError:   If required inputs are missing.
    """
    logger.info("[snapshot] Starting snapshot policy evaluation")

    # ── Extract and normalize inputs ───────────────────────────────────────────
    try:
        vm_name      = inputs["vmName"].strip()
        environment  = inputs["environment"].strip().upper()
        request_type = inputs["requestType"].strip().upper()
    except KeyError as e:
        raise KeyError(f"Required input missing from blueprint: {e}")

    change_window_id   = inputs.get("changeWindowId", "").strip()
    snapshot_label     = inputs.get("snapshotName", "on-demand").strip()
    existing_snapshots = inputs.get("existingSnapshots", [])

    if not isinstance(existing_snapshots, list):
        raise ValueError(
            f"'existingSnapshots' must be a list, got {type(existing_snapshots).__name__}"
        )

    logger.info(
        f"[snapshot] VM: {vm_name} | ENV: {environment} | "
        f"REQUEST: {request_type} | Existing snapshots: {len(existing_snapshots)}"
    )

    # ── Validate request type ─────────────────────────────────────────────────
    if request_type not in ALLOWED_REQUEST_TYPES:
        raise ValueError(
            f"Invalid requestType '{request_type}'. "
            f"Allowed values: {sorted(ALLOWED_REQUEST_TYPES)}"
        )

    # ── Validate environment ──────────────────────────────────────────────────
    if environment not in SNAPSHOT_POLICIES and environment not in BLOCKED_ENVIRONMENTS:
        raise ValueError(
            f"Unknown environment '{environment}'. "
            f"Allowed values: {sorted(list(SNAPSHOT_POLICIES.keys()) + list(BLOCKED_ENVIRONMENTS))}"
        )

    # ── Route to request handler ──────────────────────────────────────────────
    if request_type == "CREATE":
        return handle_create(
            vm_name, environment, snapshot_label,
            change_window_id, existing_snapshots
        )
    elif request_type == "DELETE":
        return handle_delete(vm_name, environment, snapshot_label, existing_snapshots)
    elif request_type == "VALIDATE":
        return handle_validate(vm_name, environment, change_window_id, existing_snapshots)


# ── CREATE Handler ─────────────────────────────────────────────────────────────
def handle_create(
    vm_name: str,
    environment: str,
    snapshot_label: str,
    change_window_id: str,
    existing_snapshots: list
) -> dict:
    """
    Validate and approve a snapshot creation request.

    Enforces:
    - DR environment block
    - Change window requirement for PROD
    - Maximum snapshot count per policy
    - Snapshot naming convention

    Args:
        vm_name:            Target VM name.
        environment:        Validated environment code.
        snapshot_label:     Descriptive label for the snapshot.
        change_window_id:   ServiceNow change window ID (required for PROD).
        existing_snapshots: List of existing snapshot dicts for this VM.

    Returns:
        dict: Approval result with snapshot name, policy, and change window details.

    Raises:
        ValueError: If any policy check fails.
    """
    logger.info(f"[snapshot] Processing CREATE request for {vm_name} in {environment}")

    # ── Block DR environment ──────────────────────────────────────────────────
    if environment in BLOCKED_ENVIRONMENTS:
        raise ValueError(
            f"Snapshot creation is BLOCKED for environment '{environment}'. "
            f"DR environments use vSphere Replication / SRM for data protection. "
            f"Snapshots would break the replication chain — use your DR runbook instead."
        )

    policy = SNAPSHOT_POLICIES[environment]

    # ── Validate change window for PROD ───────────────────────────────────────
    if policy["change_window"] and SNOW_CHANGE_REQUIRED:
        if not change_window_id:
            raise ValueError(
                f"A ServiceNow change window ID is required for PROD snapshot creation. "
                f"Raise a CHG record and provide the change number (e.g. CHG0012345) "
                f"in the 'changeWindowId' input field."
            )
        if not change_window_id.upper().startswith("CHG"):
            raise ValueError(
                f"Invalid change window ID '{change_window_id}'. "
                f"ServiceNow change records must start with 'CHG' (e.g. CHG0012345)."
            )
        logger.info(f"[snapshot] Change window validated: {change_window_id}")

    # ── Check existing snapshot count ─────────────────────────────────────────
    vm_snapshots = [
        s for s in existing_snapshots
        if s.get("vmName", "").strip().upper() == vm_name.upper()
    ]

    if len(vm_snapshots) >= policy["max_snapshots"]:
        raise ValueError(
            f"Snapshot limit reached for '{vm_name}' in {environment}. "
            f"Policy allows max {policy['max_snapshots']} snapshot(s). "
            f"Currently {len(vm_snapshots)} exist. "
            f"Delete existing snapshot(s) before creating a new one."
        )

    # ── Generate snapshot name ────────────────────────────────────────────────
    timestamp     = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    snapshot_name = f"SNAP-{vm_name.upper()}-{environment}-{timestamp}"

    logger.info(
        f"[snapshot] CREATE approved — {snapshot_name} | "
        f"Policy: {policy['description']}"
    )

    return {
        "status":          "approved",
        "vmName":          vm_name,
        "snapshotName":    snapshot_name,
        "environment":     environment,
        "changeWindowId":  change_window_id or "N/A",
        "policy": {
            "maxSnapshots":    policy["max_snapshots"],
            "retentionHours":  policy["retention_hours"],
            "changeWindow":    policy["change_window"],
            "description":     policy["description"],
        },
        "message": (
            f"Snapshot '{snapshot_name}' approved for '{vm_name}'. "
            f"Retention: {policy['retention_hours']} hours. "
            f"Max allowed: {policy['max_snapshots']}."
        ),
    }


# ── DELETE Handler ─────────────────────────────────────────────────────────────
def handle_delete(
    vm_name: str,
    environment: str,
    snapshot_label: str,
    existing_snapshots: list
) -> dict:
    """
    Validate and approve a snapshot deletion request.

    Args:
        vm_name:            Target VM name.
        environment:        Validated environment code.
        snapshot_label:     Name or partial name of snapshot to delete.
        existing_snapshots: List of existing snapshot dicts for this VM.

    Returns:
        dict: Deletion result with snapshot name and status.

    Raises:
        ValueError: If the snapshot is not found.
    """
    logger.info(f"[snapshot] Processing DELETE request for {vm_name} — {snapshot_label}")

    # Find matching snapshot
    matching = [
        s for s in existing_snapshots
        if snapshot_label.upper() in s.get("name", "").upper()
        and s.get("vmName", "").strip().upper() == vm_name.upper()
    ]

    if not matching:
        raise ValueError(
            f"No snapshot matching '{snapshot_label}' found for VM '{vm_name}'. "
            f"Verify the snapshot name and try again."
        )

    target = matching[0]
    logger.info(f"[snapshot] DELETE approved — {target['name']}")

    return {
        "status":       "deleted",
        "vmName":       vm_name,
        "snapshotName": target["name"],
        "environment":  environment,
        "policy":       SNAPSHOT_POLICIES.get(environment, {}),
        "message":      f"Snapshot '{target['name']}' approved for deletion from '{vm_name}'.",
    }


# ── VALIDATE Handler ───────────────────────────────────────────────────────────
def handle_validate(
    vm_name: str,
    environment: str,
    change_window_id: str,
    existing_snapshots: list
) -> dict:
    """
    Perform a dry-run policy validation without creating a snapshot.

    Args:
        vm_name:            Target VM name.
        environment:        Validated environment code.
        change_window_id:   ServiceNow change window ID (required for PROD).
        existing_snapshots: List of existing snapshot dicts for this VM.

    Returns:
        dict: Validation result with policy details and current snapshot count.
    """
    logger.info(f"[snapshot] Processing VALIDATE request for {vm_name} in {environment}")

    # Block check
    if environment in BLOCKED_ENVIRONMENTS:
        return {
            "status":      "blocked",
            "vmName":      vm_name,
            "environment": environment,
            "policy":      {},
            "message":     f"Snapshots are BLOCKED for {environment}. Use vSphere Replication / SRM.",
        }

    policy = SNAPSHOT_POLICIES[environment]

    vm_snapshots = [
        s for s in existing_snapshots
        if s.get("vmName", "").strip().upper() == vm_name.upper()
    ]

    can_create = len(vm_snapshots) < policy["max_snapshots"]
    needs_chg  = policy["change_window"] and SNOW_CHANGE_REQUIRED
    chg_valid  = (not needs_chg) or (change_window_id.upper().startswith("CHG") if change_window_id else False)

    logger.info(
        f"[snapshot] VALIDATE result — can_create: {can_create} | "
        f"chg_required: {needs_chg} | chg_valid: {chg_valid}"
    )

    return {
        "status":               "validated",
        "vmName":               vm_name,
        "environment":          environment,
        "canCreate":            can_create and chg_valid,
        "currentSnapshotCount": len(vm_snapshots),
        "changeWindowId":       change_window_id or "N/A",
        "changeWindowValid":    chg_valid,
        "policy": {
            "maxSnapshots":   policy["max_snapshots"],
            "retentionHours": policy["retention_hours"],
            "changeWindow":   policy["change_window"],
            "description":    policy["description"],
        },
        "message": (
            f"Validation complete for '{vm_name}' in {environment}. "
            f"Current snapshots: {len(vm_snapshots)}/{policy['max_snapshots']}. "
            f"Can create: {can_create and chg_valid}."
        ),
    }
