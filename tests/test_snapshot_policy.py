"""
test_snapshot_policy.py
-----------------------
Unit tests for the Aria Automation ABX Action — Snapshot Policy Enforcement.

Tests cover CREATE, DELETE, and VALIDATE request types across all
environment tiers, change window enforcement, and all error paths.

Run with:
    pytest tests/test_snapshot_policy.py -v
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../abx-actions'))
import snapshot_policy


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def disable_snow_requirement(monkeypatch):
    """Disable ServiceNow change window requirement for most tests."""
    monkeypatch.setattr(snapshot_policy, "SNOW_CHANGE_REQUIRED", False)


@pytest.fixture
def enable_snow_requirement(monkeypatch):
    """Enable ServiceNow change window requirement for PROD tests."""
    monkeypatch.setattr(snapshot_policy, "SNOW_CHANGE_REQUIRED", True)


@pytest.fixture
def base_inputs():
    """Base valid inputs for a DEV CREATE request."""
    return {
        "vmName":            "web-server-01",
        "environment":       "DEV",
        "requestType":       "CREATE",
        "snapshotName":      "pre-patch",
        "existingSnapshots": [],
    }


# ── handler() routing tests ────────────────────────────────────────────────────

class TestHandlerRouting:

    def test_routes_create_request(self, base_inputs):
        """handler() should route CREATE to handle_create."""
        result = snapshot_policy.handler(context=None, inputs=base_inputs)
        assert result["status"] == "approved"

    def test_routes_validate_request(self, base_inputs):
        """handler() should route VALIDATE to handle_validate."""
        base_inputs["requestType"] = "VALIDATE"
        result = snapshot_policy.handler(context=None, inputs=base_inputs)
        assert result["status"] == "validated"

    def test_routes_delete_request(self, base_inputs):
        """handler() should route DELETE to handle_delete."""
        base_inputs["requestType"]       = "DELETE"
        base_inputs["snapshotName"]      = "SNAP-WEB-SERVER-01"
        base_inputs["existingSnapshots"] = [
            {"vmName": "web-server-01", "name": "SNAP-WEB-SERVER-01-DEV-20260101-120000"}
        ]
        result = snapshot_policy.handler(context=None, inputs=base_inputs)
        assert result["status"] == "deleted"

    def test_raises_on_invalid_request_type(self, base_inputs):
        """handler() should raise ValueError for unknown requestType."""
        base_inputs["requestType"] = "RESTORE"
        with pytest.raises(ValueError, match="Invalid requestType"):
            snapshot_policy.handler(context=None, inputs=base_inputs)

    def test_raises_on_missing_vm_name(self, base_inputs):
        """handler() should raise KeyError if vmName missing."""
        del base_inputs["vmName"]
        with pytest.raises(KeyError):
            snapshot_policy.handler(context=None, inputs=base_inputs)

    def test_raises_on_unknown_environment(self, base_inputs):
        """handler() should raise ValueError for unknown environment."""
        base_inputs["environment"] = "STAGE2"
        with pytest.raises(ValueError, match="Unknown environment"):
            snapshot_policy.handler(context=None, inputs=base_inputs)

    def test_normalizes_lowercase_inputs(self, base_inputs):
        """handler() should normalize lowercase environment and requestType."""
        base_inputs["environment"] = "dev"
        base_inputs["requestType"] = "create"
        result = snapshot_policy.handler(context=None, inputs=base_inputs)
        assert result["status"] == "approved"


# ── CREATE tests ───────────────────────────────────────────────────────────────

class TestHandleCreate:

    def test_create_approved_for_dev(self, base_inputs):
        """CREATE should be approved for DEV with no existing snapshots."""
        result = snapshot_policy.handler(context=None, inputs=base_inputs)
        assert result["status"]      == "approved"
        assert result["vmName"]      == "web-server-01"
        assert result["environment"] == "DEV"
        assert "SNAP-WEB-SERVER-01-DEV-" in result["snapshotName"]

    def test_create_approved_for_test(self, base_inputs):
        """CREATE should be approved for TEST environment."""
        base_inputs["environment"] = "TEST"
        result = snapshot_policy.handler(context=None, inputs=base_inputs)
        assert result["status"] == "approved"

    def test_create_approved_for_uat(self, base_inputs):
        """CREATE should be approved for UAT with no existing snapshots."""
        base_inputs["environment"] = "UAT"
        result = snapshot_policy.handler(context=None, inputs=base_inputs)
        assert result["status"] == "approved"
        assert result["policy"]["maxSnapshots"] == 1

    def test_create_blocked_for_dr(self, base_inputs):
        """CREATE should be blocked for DR environment."""
        base_inputs["environment"] = "DR"
        with pytest.raises(ValueError, match="BLOCKED"):
            snapshot_policy.handler(context=None, inputs=base_inputs)

    def test_create_blocked_when_max_snapshots_reached(self, base_inputs):
        """CREATE should be blocked when max snapshot count is reached."""
        base_inputs["existingSnapshots"] = [
            {"vmName": "web-server-01", "name": "SNAP-WEB-SERVER-01-DEV-001"},
            {"vmName": "web-server-01", "name": "SNAP-WEB-SERVER-01-DEV-002"},
            {"vmName": "web-server-01", "name": "SNAP-WEB-SERVER-01-DEV-003"},
        ]
        with pytest.raises(ValueError, match="Snapshot limit reached"):
            snapshot_policy.handler(context=None, inputs=base_inputs)

    def test_create_ignores_other_vm_snapshots(self, base_inputs):
        """CREATE should ignore snapshots belonging to other VMs."""
        base_inputs["existingSnapshots"] = [
            {"vmName": "other-vm-01", "name": "SNAP-OTHER-VM-01-DEV-001"},
            {"vmName": "other-vm-02", "name": "SNAP-OTHER-VM-02-DEV-001"},
        ]
        result = snapshot_policy.handler(context=None, inputs=base_inputs)
        assert result["status"] == "approved"

    def test_snapshot_name_includes_timestamp(self, base_inputs):
        """CREATE should generate snapshot name with timestamp."""
        result = snapshot_policy.handler(context=None, inputs=base_inputs)
        name = result["snapshotName"]
        assert name.startswith("SNAP-WEB-SERVER-01-DEV-")
        # Timestamp portion should be 15 chars: YYYYMMDD-HHMMSS
        timestamp_part = name.split("DEV-")[1]
        assert len(timestamp_part) == 15

    def test_create_returns_policy_details(self, base_inputs):
        """CREATE result should include full policy details."""
        result = snapshot_policy.handler(context=None, inputs=base_inputs)
        assert "maxSnapshots"   in result["policy"]
        assert "retentionHours" in result["policy"]
        assert "changeWindow"   in result["policy"]
        assert "description"    in result["policy"]


# ── PROD change window tests ───────────────────────────────────────────────────

class TestProdChangeWindow:

    def test_prod_create_requires_change_window(self, base_inputs, enable_snow_requirement):
        """PROD CREATE should raise if no changeWindowId provided."""
        base_inputs["environment"] = "PROD"
        with pytest.raises(ValueError, match="change window ID is required"):
            snapshot_policy.handler(context=None, inputs=base_inputs)

    def test_prod_create_approved_with_valid_chg(self, base_inputs, enable_snow_requirement):
        """PROD CREATE should be approved with valid CHG number."""
        base_inputs["environment"]    = "PROD"
        base_inputs["changeWindowId"] = "CHG0012345"
        result = snapshot_policy.handler(context=None, inputs=base_inputs)
        assert result["status"]        == "approved"
        assert result["changeWindowId"] == "CHG0012345"

    def test_prod_create_rejects_invalid_chg_format(self, base_inputs, enable_snow_requirement):
        """PROD CREATE should reject changeWindowId not starting with CHG."""
        base_inputs["environment"]    = "PROD"
        base_inputs["changeWindowId"] = "INC0012345"
        with pytest.raises(ValueError, match="must start with 'CHG'"):
            snapshot_policy.handler(context=None, inputs=base_inputs)

    def test_prod_max_snapshots_is_one(self, base_inputs):
        """PROD policy should allow max 1 snapshot."""
        base_inputs["environment"]       = "PROD"
        base_inputs["existingSnapshots"] = [
            {"vmName": "web-server-01", "name": "SNAP-WEB-SERVER-01-PROD-001"}
        ]
        with pytest.raises(ValueError, match="Snapshot limit reached"):
            snapshot_policy.handler(context=None, inputs=base_inputs)


# ── DELETE tests ───────────────────────────────────────────────────────────────

class TestHandleDelete:

    def test_delete_approved_when_snapshot_found(self, base_inputs):
        """DELETE should succeed when matching snapshot exists."""
        base_inputs["requestType"]       = "DELETE"
        base_inputs["snapshotName"]      = "SNAP-WEB-SERVER-01"
        base_inputs["existingSnapshots"] = [
            {"vmName": "web-server-01", "name": "SNAP-WEB-SERVER-01-DEV-20260101-120000"}
        ]
        result = snapshot_policy.handler(context=None, inputs=base_inputs)
        assert result["status"] == "deleted"
        assert "SNAP-WEB-SERVER-01" in result["snapshotName"]

    def test_delete_raises_when_not_found(self, base_inputs):
        """DELETE should raise ValueError if snapshot not found."""
        base_inputs["requestType"]       = "DELETE"
        base_inputs["snapshotName"]      = "SNAP-NONEXISTENT"
        base_inputs["existingSnapshots"] = []
        with pytest.raises(ValueError, match="No snapshot matching"):
            snapshot_policy.handler(context=None, inputs=base_inputs)


# ── VALIDATE tests ─────────────────────────────────────────────────────────────

class TestHandleValidate:

    def test_validate_returns_can_create_true(self, base_inputs):
        """VALIDATE should return canCreate True when under limit."""
        base_inputs["requestType"] = "VALIDATE"
        result = snapshot_policy.handler(context=None, inputs=base_inputs)
        assert result["status"]    == "validated"
        assert result["canCreate"] == True
        assert result["currentSnapshotCount"] == 0

    def test_validate_returns_can_create_false_at_limit(self, base_inputs):
        """VALIDATE should return canCreate False when at snapshot limit."""
        base_inputs["requestType"]       = "VALIDATE"
        base_inputs["existingSnapshots"] = [
            {"vmName": "web-server-01", "name": "SNAP-1"},
            {"vmName": "web-server-01", "name": "SNAP-2"},
            {"vmName": "web-server-01", "name": "SNAP-3"},
        ]
        result = snapshot_policy.handler(context=None, inputs=base_inputs)
        assert result["canCreate"]            == False
        assert result["currentSnapshotCount"] == 3

    def test_validate_blocked_for_dr(self, base_inputs):
        """VALIDATE should return blocked status for DR environment."""
        base_inputs["requestType"] = "VALIDATE"
        base_inputs["environment"] = "DR"
        result = snapshot_policy.handler(context=None, inputs=base_inputs)
        assert result["status"] == "blocked"

    def test_validate_returns_policy_details(self, base_inputs):
        """VALIDATE result should include policy details."""
        base_inputs["requestType"] = "VALIDATE"
        result = snapshot_policy.handler(context=None, inputs=base_inputs)
        assert "maxSnapshots"   in result["policy"]
        assert "retentionHours" in result["policy"]
