from __future__ import annotations

import importlib

import pytest

from app.services.capabilities import (
    ApprovalMode,
    Persona,
    REGISTRY_VERSION,
    SideEffect,
    capabilities_for_personas,
    capability_allowed_for_personas,
    get_capability,
    list_capabilities,
    validate_capability_registry,
)


def _ids_for(*personas: Persona) -> set[str]:
    return {capability.id for capability in capabilities_for_personas(personas)}


def test_registry_is_internally_valid():
    validate_capability_registry()

    ids = [capability.id for capability in list_capabilities()]

    assert REGISTRY_VERSION == "2026-06-22"
    assert len(ids) == len(set(ids))
    assert all(capability.operation for capability in list_capabilities())
    assert all(capability.input_schema for capability in list_capabilities())
    assert all(capability.output_schema for capability in list_capabilities())
    assert all(capability.service_handler for capability in list_capabilities())
    assert all(capability.trace_tags["capability_id"] == capability.id for capability in list_capabilities())
    assert all(capability.trace_tags["operation"] == capability.operation.value for capability in list_capabilities())


def test_shopper_policy_excludes_operator_and_customer_lookup_capabilities():
    shopper_ids = _ids_for(Persona.SHOPPER)

    assert "public.catalog.search" in shopper_ids
    assert "public.catalog.product_detail" in shopper_ids
    assert "associate.customer.lookup" not in shopper_ids
    assert "catalog_admin.product.draft" not in shopper_ids
    assert "developer_trace.read" not in shopper_ids


def test_authenticated_shopper_inherits_public_catalog_but_not_admin_capabilities():
    shopper_ids = _ids_for(Persona.AUTHENTICATED_SHOPPER)

    assert "public.catalog.search" in shopper_ids
    assert "shopper.account.order_status" in shopper_ids
    assert "shopper.account.recommendations" in shopper_ids
    assert "associate.customer.lookup" not in shopper_ids
    assert "catalog_admin.product.publish" not in shopper_ids


def test_catalog_admin_and_developer_trace_are_separate_grants():
    catalog_admin_ids = _ids_for(Persona.CATALOG_ADMIN)
    trace_ids = _ids_for(Persona.DEVELOPER_TRACE)

    assert "catalog_admin.product.draft" in catalog_admin_ids
    assert "catalog_admin.product.publish" in catalog_admin_ids
    assert "developer_trace.read" not in catalog_admin_ids
    assert trace_ids == {"developer_trace.read"}


def test_send_capabilities_require_explicit_approval_and_send_capable_grant():
    send_capabilities = [
        capability
        for capability in list_capabilities()
        if capability.side_effect == SideEffect.SEND
    ]

    assert send_capabilities
    for capability in send_capabilities:
        assert capability.approval_mode == ApprovalMode.EXPLICIT_BOOLEAN
        assert capability.approval_field == "approved"
        assert Persona.SEND_CAPABLE in capability.required_grants
        assert not capability_allowed_for_personas(capability, capability.allowed_personas)


def test_send_grant_must_be_combined_with_actor_persona():
    send_email = get_capability("associate.customer.email.send")

    assert not capability_allowed_for_personas(send_email, (Persona.SEND_CAPABLE,))
    assert not capability_allowed_for_personas(send_email, (Persona.ASSOCIATE,))
    assert capability_allowed_for_personas(
        send_email,
        (Persona.ASSOCIATE, Persona.SEND_CAPABLE),
    )


def test_unknown_capability_raises_clear_error():
    with pytest.raises(ValueError, match="Unknown capability"):
        get_capability("missing.capability")


def test_catalog_admin_session_capability_references_resolvable_symbols():
    capability = get_capability("catalog_admin.session")

    for dotted_path in (capability.output_schema, capability.service_handler):
        module_name, symbol_name = dotted_path.rsplit(".", 1)
        module = importlib.import_module(module_name)
        assert getattr(module, symbol_name)
