"""Deterministic tests for the Backend Output Boundary Observe detector."""

from app.services.output_assurance import OutputBoundaryContext, observe_recommendation_candidate


class FixtureCatalog:
    def __init__(self):
        self.actions = [{"package": "Excel_MS", "action": "GoToCell", "parameters": []}]

    def iter_action_schemas(self):
        yield from self.actions

    def get_action_schema(self, package, action):
        return next(
            (item for item in self.actions if (item["package"], item["action"]) == (package, action)),
            None,
        )


class BrokenCatalog:
    def iter_action_schemas(self):
        raise ConnectionError("catalog unavailable")


def recommendation(package="Excel_MS", action="GoToCell"):
    return {
        "schema_version": "1.0",
        "steps": [{
            "step_id": "step-1",
            "actions": [{
                "order": 1,
                "package": package,
                "action": action,
                "parameters": [],
                "children": [],
            }],
        }],
        "variables": [],
    }


def context(**changes):
    values = {
        "session_id": "session-1",
        "request_id": "request-1",
        "source": "chat",
        "requested_agent_version": "v3",
        "resolved_agent_version": "v3",
        "agent_registry_snapshot": {"versions": [{"id": "v3"}], "default": "v3"},
    }
    values.update(changes)
    return OutputBoundaryContext(**values)


def test_known_action_is_allow_candidate_but_never_validated():
    result = observe_recommendation_candidate(recommendation(), context(), catalog=FixtureCatalog())

    assert result["decision"] == "allow_candidate"
    assert result["validated"] is False
    assert result["assurance_status"] == "unassured_observe"
    assert [item["status"] for item in result["controls"]] == ["pass", "pass"]


def test_made_up_package_and_action_are_denied_by_backend_detector():
    result = observe_recommendation_candidate(
        recommendation("MadeUpPackage", "InventedAction"), context(), catalog=FixtureCatalog()
    )

    assert result["decision"] == "deny"
    assert result["boundary_findings"] == [{
        "control": "catalog_closure",
        "code": "UNKNOWN_CATALOG_ACTION",
        "path": "recommendation.steps[0].actions[0]",
        "message": "카탈로그에 없는 액션: MadeUpPackage/InventedAction",
    }]


def test_unknown_payload_field_is_a_strict_schema_denial():
    payload = recommendation()
    payload["unexpected"] = True

    result = observe_recommendation_candidate(payload, context(), catalog=FixtureCatalog())

    assert result["decision"] == "deny"
    assert any(item["code"] == "UNKNOWN_FIELD" for item in result["boundary_findings"])


def test_unknown_nested_action_field_is_a_strict_schema_denial():
    payload = recommendation()
    payload["steps"][0]["actions"][0]["invented_runtime_flag"] = True

    result = observe_recommendation_candidate(payload, context(), catalog=FixtureCatalog())

    assert result["decision"] == "deny"
    assert any(
        item["path"] == "recommendation.steps[0].actions[0].invented_runtime_flag"
        for item in result["boundary_findings"]
    )


def test_documented_flow_editor_metadata_is_allowed():
    payload = recommendation()
    payload["steps"][0].update({"x": 10, "y": 20, "collapsed": False})
    payload["steps"][0]["actions"][0]["_uiKey"] = "node-1"

    result = observe_recommendation_candidate(payload, context(source="drag"), catalog=FixtureCatalog())

    assert result["decision"] == "allow_candidate"


def test_detector_error_is_unassured_not_allow():
    result = observe_recommendation_candidate(recommendation(), context(), catalog=BrokenCatalog())

    assert result["decision"] == "unassured"
    assert result["controls"][1] == {
        "control_id": "catalog_closure", "status": "error", "error_type": "ConnectionError"
    }


def test_identity_is_deterministic_and_producer_advisory_is_not_reflected():
    ctx = context(producer_advisory={"violations": ["private customer text"]})

    first = observe_recommendation_candidate(recommendation(), ctx, catalog=FixtureCatalog())
    second = observe_recommendation_candidate(recommendation(), ctx, catalog=FixtureCatalog())

    assert first["candidate_id"] == second["candidate_id"]
    assert first["observation_id"] == second["observation_id"]
    assert first["producer_advisory"]["present"] is True
    assert "private customer text" not in str(first)


def test_agent_provenance_records_missing_resolved_version_without_guessing():
    result = observe_recommendation_candidate(
        recommendation(), context(resolved_agent_version=None), catalog=FixtureCatalog()
    )

    provenance = result["agent_provenance"]
    assert provenance["requested_agent_version"] == "v3"
    assert provenance["resolved_agent_version"] is None
    assert provenance["resolved_version_observability"] == "not_observable"
    assert provenance["agent_registry_digest"].startswith("sha256:")
