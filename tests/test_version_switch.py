import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, InvalidTransition, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class VersionSwitchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.analyst = Actor("analyst", "analyst")
        self.authorizer = Actor("authorizer", "authorizer")
        self.metrology = Actor("metrology", "metrology")

    def tearDown(self):
        self.tmp.cleanup()

    def _instrument(self, due_at="2099-01-01"):
        entity = self.service.create(
            self.admin, "instrument", {"name": "Analyzer", "serial": "A-1"}
        )
        self.service.transition(self.admin, entity["id"], "send_calibration", {})
        self.service.transition(
            self.metrology,
            entity["id"],
            "calibrate",
            {"due_at": due_at, "passed": True},
        )
        return entity["id"]

    def _method(self, name, version, instrument_id):
        entity = self.service.create(
            self.authorizer, "method", {"name": name, "version": version}
        )
        self.service.transition(
            self.authorizer,
            entity["id"],
            "validate_method",
            {"parameters": {"range": [0, 10]}, "instrument_ids": [instrument_id]},
        )
        return entity["id"]

    def _release(self, result_id, instrument_id, method_id, value=4.2):
        return self.service.transition(
            self.analyst,
            result_id,
            "release",
            {
                "instrument_id": instrument_id,
                "method_id": method_id,
                "value": value,
                "unit": "mg/L",
            },
        )

    def _failed_release_audits(self, result_id):
        return [
            entry
            for entry in self.service.audit_log(result_id)
            if entry["action"] == "release_failed"
        ]

    def test_version_switch_retires_old_and_flags_review(self):
        instrument = self._instrument()
        m1 = self._method("Assay-A", "v1", instrument)
        result = self.service.create(
            self.analyst,
            "result",
            {"sample_id": "S-1", "measurement": "m", "method_id": m1},
        )
        m2 = self._method("Assay-A", "v2", instrument)

        old = self.service.get(m1)
        self.assertEqual(old["status"], "retired")
        self.assertEqual(old["data"]["superseded_by"], m2)
        self.assertEqual(
            [m["id"] for m in self.service.effective_methods()], [m2]
        )
        self.assertEqual(
            [m["id"] for m in self.service.effective_methods(name="Assay-A")], [m2]
        )

        flagged = self.service.get(result["id"])
        self.assertEqual(flagged["status"], "review")
        self.assertEqual(flagged["data"]["review_reason"], "method version retired")

        with self.assertRaises(InvalidTransition):
            self._release(result["id"], instrument, m2)

        reassigned = self.service.transition(
            self.analyst, result["id"], "assign_method", {"method_id": m2}
        )
        self.assertEqual(reassigned["status"], "pending")
        self.assertEqual(reassigned["data"]["method_version"], "v2")

        released = self._release(result["id"], instrument, m2)
        self.assertEqual(released["status"], "released")
        self.assertEqual(released["data"]["instrument_name"], "Analyzer")
        self.assertEqual(released["data"]["instrument_serial"], "A-1")
        self.assertEqual(released["data"]["method_name"], "Assay-A")
        self.assertEqual(released["data"]["method_version"], "v2")
        self.assertEqual(released["data"]["value"], 4.2)
        self.assertEqual(released["data"]["unit"], "mg/L")
        self.assertEqual(released["data"]["released_by"], "analyst")
        self.assertTrue(released["data"]["released_at"])

        actions = [
            entry["action"] for entry in self.service.audit_log(m1)
        ]
        self.assertIn("auto_retire", actions)
        actions = [
            entry["action"] for entry in self.service.audit_log(result["id"])
        ]
        self.assertIn("enter_review", actions)

    def test_released_result_snapshot_is_frozen(self):
        instrument = self._instrument()
        m1 = self._method("Assay-A", "v1", instrument)
        result = self.service.create(
            self.analyst, "result", {"sample_id": "S-1", "measurement": "m"}
        )
        self._release(result["id"], instrument, m1)

        # Retire the method afterwards: the released snapshot must not change.
        self._method("Assay-A", "v2", instrument)
        released = self.service.get(result["id"])
        self.assertEqual(released["status"], "released")
        self.assertEqual(released["data"]["method_id"], m1)
        self.assertEqual(released["data"]["method_name"], "Assay-A")
        self.assertEqual(released["data"]["method_version"], "v1")
        self.assertEqual(released["data"]["released_by"], "analyst")

    def test_release_with_retired_method_rejected_and_audited(self):
        instrument = self._instrument()
        m1 = self._method("Assay-A", "v1", instrument)
        self._method("Assay-A", "v2", instrument)
        result = self.service.create(
            self.analyst, "result", {"sample_id": "S-1", "measurement": "m"}
        )
        with self.assertRaises(ValidationError):
            self._release(result["id"], instrument, m1)
        failed = self._failed_release_audits(result["id"])
        self.assertEqual(len(failed), 1)
        self.assertIn("validated", failed[0]["detail"]["reason"])

    def test_quarantined_instrument_release_rejected_and_audited(self):
        instrument = self._instrument()
        m1 = self._method("Assay-A", "v1", instrument)
        result = self.service.create(
            self.analyst, "result", {"sample_id": "S-1", "measurement": "m"}
        )
        self.service.transition(
            self.metrology, instrument, "quarantine", {"reason": "contamination"}
        )
        with self.assertRaises(ValidationError):
            self._release(result["id"], instrument, m1)
        failed = self._failed_release_audits(result["id"])
        self.assertEqual(len(failed), 1)
        self.assertIn("quarantined", failed[0]["detail"]["reason"])
        self.assertEqual(failed[0]["actor_id"], "analyst")

    def test_expired_calibration_release_rejected_and_audited(self):
        instrument = self._instrument(due_at="2020-01-01")
        m1 = self._method("Assay-A", "v1", instrument)
        result = self.service.create(
            self.analyst, "result", {"sample_id": "S-1", "measurement": "m"}
        )
        with self.assertRaises(ValidationError):
            self._release(result["id"], instrument, m1)
        failed = self._failed_release_audits(result["id"])
        self.assertEqual(len(failed), 1)
        self.assertIn("calibration is not current", failed[0]["detail"]["reason"])

    def test_assign_method_permissions_and_validation(self):
        instrument = self._instrument()
        m1 = self._method("Assay-A", "v1", instrument)
        draft = self.service.create(
            self.authorizer, "method", {"name": "Assay-B", "version": "v1"}
        )
        result = self.service.create(
            self.analyst, "result", {"sample_id": "S-1", "measurement": "m"}
        )
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("viewer", "viewer"),
                result["id"],
                "assign_method",
                {"method_id": m1},
            )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.analyst, result["id"], "assign_method", {"method_id": draft["id"]}
            )
        with self.assertRaises(ValidationError):
            self.service.create(
                self.analyst,
                "result",
                {"sample_id": "S-2", "measurement": "m", "method_id": draft["id"]},
            )

    def test_revoked_method_flags_pending_results(self):
        instrument = self._instrument()
        m1 = self._method("Assay-A", "v1", instrument)
        result = self.service.create(
            self.analyst,
            "result",
            {"sample_id": "S-1", "measurement": "m", "method_id": m1},
        )
        self.service.transition(
            self.authorizer, m1, "revoke_method", {"reason": "no longer valid"}
        )
        self.assertEqual(self.service.get(result["id"])["status"], "review")
        self.assertEqual(self.service.effective_methods(), [])


if __name__ == "__main__":
    unittest.main()
