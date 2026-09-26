from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, DomainError, NotFoundError
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        try:
            next_status, patch = self.rules.validate_transition(
                actor, entity, action, dict(data or {}), self._lookup
            )
        except DomainError as exc:
            if self.rules.normalize_kind(entity["kind"]) == "result" and action == "release":
                self.audit.record(
                    entity_id,
                    actor,
                    "release_rejected",
                    entity["status"],
                    entity["status"],
                    {"reason": str(exc)},
                )
            raise
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        self._after_transition(actor, updated, action)
        return updated

    def _after_transition(self, actor, entity, action):
        kind = self.rules.normalize_kind(entity["kind"])
        if kind == "method" and action == "validate_method":
            self._retire_superseded_methods(actor, entity)
        if kind == "method" and action == "revoke_method":
            self._flag_results_for_review(actor, entity, "method revoked")

    def _retire_superseded_methods(self, actor, method):
        name = method["data"].get("name")
        for other in self.repository.find_entities("method", "name", name):
            if other["id"] == method["id"] or other["status"] != "validated":
                continue
            data = dict(other["data"])
            data.update(
                {
                    "retired_by": actor.user_id,
                    "retired_reason": "superseded by %s" % method["data"].get("version"),
                    "replaced_by": method["id"],
                }
            )
            retired = self.repository.update_entity(
                other["id"], other["version"], "superseded", data
            )
            self.audit.record(
                other["id"],
                actor,
                "auto_retire",
                "validated",
                "superseded",
                {"replaced_by": method["id"]},
            )
            self._flag_results_for_review(actor, retired, "method superseded")

    def _flag_results_for_review(self, actor, method, reason):
        for result in self.repository.find_entities("result", "method_id", method["id"]):
            if result["status"] != "pending":
                continue
            data = dict(result["data"])
            data["review_reason"] = reason
            self.repository.update_entity(result["id"], result["version"], "review", data)
            self.audit.record(
                result["id"],
                actor,
                "enter_review",
                "pending",
                "review",
                {"method_id": method["id"], "reason": reason},
            )

    def effective_methods(self):
        effective = {}
        for method in self.repository.list_entities(kind="method"):
            if method["status"] == "validated":
                effective[method["data"].get("name")] = method
        return {"items": list(effective.values())}

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
