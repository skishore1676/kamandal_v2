from kamandal_v2.tools.export_tradelab_capabilities import build_manifest


def test_tradelab_export_is_code_only_and_broker_inert() -> None:
    manifest = build_manifest()
    capability = manifest["capabilities"][0]

    assert manifest["schema"] == "kamandal.planner_capabilities.v1"
    assert {"put_spread", "call_spread"}.issubset(capability["supported_structures"])
    assert capability["operationally_available"] is False
    assert capability["parameter_constraints"]["automatic_shadow_allowed"] is False
    assert not any(manifest["protected_effects_performed"].values())
