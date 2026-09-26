import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, InvalidTransition, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class VersioningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.instrument = self._make_instrument()

    def tearDown(self):
        self.tmp.cleanup()

    def _make_instrument(self, due_at="2099-01-01"):
        instrument = self.service.create(
            self.admin, "instrument", {"name": "Analyzer", "serial": "A-1"}
        )
        self.service.transition(self.admin, instrument["id"], "send_calibration", {})
        return self.service.transition(
            self.admin, instrument["id"], "calibrate", {"due_at": due_at, "passed": True}
        )

    def _make_method(self, name, version):
        method = self.service.create(
            self.admin, "method", {"name": name, "version": version}
        )
        return self.service.transition(
            self.admin,
            method["id"],
            "validate_method",
            {"parameters": {"range": [0, 10]}, "instrument_ids": [self.instrument["id"]]},
        )

    def _make_result(self, method):
        return self.service.create(
            self.admin,
            "result",
            {
                "sample_id": "S-1",
                "measurement": "initial",
                "instrument_id": self.instrument["id"],
                "method_id": method["id"],
            },
        )

    def test_new_version_retires_old_and_effective_shown(self):
        v1 = self._make_method("Assay-A", "v1")
        v2 = self._make_method("Assay-A", "v2")
        old = self.service.get(v1["id"])
        self.assertEqual(old["status"], "superseded")
        self.assertEqual(old["data"]["replaced_by"], v2["id"])
        effective = self.service.effective_methods()["items"]
        self.assertEqual([m["id"] for m in effective], [v2["id"]])
        actions = [row["action"] for row in self.service.audit_log(v1["id"])]
        self.assertIn("auto_retire", actions)

    def test_pending_result_enters_review_then_reassign_and_release(self):
        v1 = self._make_method("Assay-A", "v1")
        result = self._make_result(v1)
        v2 = self._make_method("Assay-A", "v2")

        pending = self.service.get(result["id"])
        self.assertEqual(pending["status"], "review")
        self.assertEqual(pending["data"]["review_reason"], "method superseded")

        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.admin, result["id"], "release", {"value": 4.2, "unit": "mg/L"}
            )

        reassigned = self.service.transition(
            self.admin, result["id"], "reassign_method", {"method_id": v2["id"]}
        )
        self.assertEqual(reassigned["status"], "pending")

        released = self.service.transition(
            self.admin, result["id"], "release", {"value": 4.2, "unit": "mg/L"}
        )
        self.assertEqual(released["status"], "released")
        self.assertEqual(released["data"]["method_id"], v2["id"])
        self.assertEqual(released["data"]["release_snapshot"]["method_version"], "v2")

    def test_reassign_requires_effective_method_and_role(self):
        v1 = self._make_method("Assay-A", "v1")
        result = self._make_result(v1)
        self._make_method("Assay-A", "v2")
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin, result["id"], "reassign_method", {"method_id": v1["id"]}
            )
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("viewer", "viewer"),
                result["id"],
                "reassign_method",
                {"method_id": v1["id"]},
            )

    def test_released_result_keeps_snapshot_after_retire(self):
        v1 = self._make_method("Assay-A", "v1")
        result = self._make_result(v1)
        self.service.transition(
            self.admin, result["id"], "release", {"value": 4.2, "unit": "mg/L"}
        )
        self._make_method("Assay-A", "v2")

        released = self.service.get(result["id"])
        self.assertEqual(released["status"], "released")
        self.assertEqual(released["data"]["method_id"], v1["id"])
        snapshot = released["data"]["release_snapshot"]
        self.assertEqual(snapshot["method_version"], "v1")
        self.assertEqual(snapshot["instrument_serial"], "A-1")
        self.assertEqual(snapshot["value"], 4.2)
        self.assertEqual(snapshot["unit"], "mg/L")
        self.assertEqual(released["data"]["released_by"], "admin")

    def test_release_rejected_and_audited_when_calibration_expired(self):
        instrument = self._make_instrument(due_at="2020-01-01")
        method = self.service.create(
            self.admin, "method", {"name": "Assay-B", "version": "v1"}
        )
        method = self.service.transition(
            self.admin,
            method["id"],
            "validate_method",
            {"parameters": {"range": [0, 10]}, "instrument_ids": [instrument["id"]]},
        )
        result = self.service.create(
            self.admin,
            "result",
            {"sample_id": "S-2", "measurement": "m", "instrument_id": instrument["id"], "method_id": method["id"]},
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin, result["id"], "release", {"value": 1.0, "unit": "mg/L"}
            )
        audit = self.service.audit_log(result["id"])
        rejected = [row for row in audit if row["action"] == "release_rejected"]
        self.assertTrue(rejected)
        self.assertIn("calibration", rejected[0]["detail"]["reason"])
        self.assertEqual(self.service.get(result["id"])["status"], "pending")

    def test_release_rejected_and_audited_when_quarantined(self):
        v1 = self._make_method("Assay-A", "v1")
        result = self._make_result(v1)
        self.service.transition(
            self.admin, self.instrument["id"], "quarantine", {"reason": "damage"}
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin, result["id"], "release", {"value": 1.0, "unit": "mg/L"}
            )
        audit = self.service.audit_log(result["id"])
        rejected = [row for row in audit if row["action"] == "release_rejected"]
        self.assertTrue(rejected)
        self.assertIn("active instrument", rejected[0]["detail"]["reason"])


if __name__ == "__main__":
    unittest.main()
