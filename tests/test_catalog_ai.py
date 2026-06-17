from __future__ import annotations

from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.catalog.ai_schemas import (
    CatalogAICommandRequest,
    CatalogAIInventoryProposal,
    CatalogAIProductProposal,
    CatalogAIVariantProposal,
)
from app.catalog.admin_schemas import DesignSpecificationDraft
from app.config import Settings
from app.database import Base
from app.models import CatalogDraftRevision, CatalogWorkflowEvent, CatalogWorkflow, Store, SyntheticRun
from app.services.auth.clerk import AuthenticatedPrincipal
from app.services.catalog_ai import CatalogAICommandError, execute_catalog_ai_command
from app.services.catalog_workflow import get_catalog_workflow_projection, start_catalog_workflow


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    testing_session = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    Base.metadata.create_all(engine)
    session = testing_session()
    now = datetime(2026, 6, 16, tzinfo=timezone.utc)
    session.add(
        SyntheticRun(
            id="run_catalog", seed=101, status="loaded", started_at=now, config={}
        )
    )
    session.add(
        Store(
            id="1001",
            seed_run_id="run_catalog",
            name="Dallas Downtown",
            city="Dallas",
            state="TX",
            postal_code="75201",
            address_line1="1 Main St",
            profile_type="texas_core",
            services=[],
            raw_source={},
        )
    )
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _settings(**overrides) -> Settings:
    defaults = {
        "database_url": "sqlite+pysqlite:///:memory:",
        "openai_api_key": "test-openai-key",
        "catalog_studio_responses_model": "gpt-5.5",
        "catalog_studio_moderation_model": "omni-moderation-latest",
        "catalog_studio_responses_timeout_seconds": 30.0,
        "catalog_studio_trace_retention_days": 7,
        "catalog_studio_trace_max_depth": 6,
        "catalog_studio_trace_max_string_length": 1000,
        "catalog_studio_trace_max_array_length": 25,
        "catalog_studio_trace_max_object_keys": 50,
        "catalog_studio_trace_max_bytes": 16384,
        "catalog_studio_trace_redacted_keys": "",
        "catalog_studio_shared_workflows": False,
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        provider="clerk",
        provider_user_id="user_admin",
        email="admin@example.com",
        claims={},
    )


def _run(db, settings: Settings, *, idempotency_key: str = "catalog-ai-workflow"):
    return start_catalog_workflow(
        db,
        principal=_principal(),
        title="Create a customer-facing product",
        business_summary="Preparing a private product draft.",
        settings=settings,
        idempotency_key=idempotency_key,
    )


def _proposal(*, title: str = "Midnight Atelier Coat", color: str = "Black"):
    return CatalogAIProductProposal(
        title=title,
        description="A sculpted wool evening coat with a clean architectural line.",
        brand="Sterling Hollis",
        category="womens_apparel",
        image_direction="Editorial studio photograph on a warm neutral backdrop.",
        design_specification=DesignSpecificationDraft(
            product_type="single-breasted coat",
            silhouette="long sculpted column",
            construction="notched collar with concealed front closure",
            distinguishing_features=["curved shoulder seam", "welt pockets"],
        ),
        variant_axes=["color"],
        primary_variant_index=0,
        variants=[
            CatalogAIVariantProposal(
                color=color,
                material="wool",
                gender="women",
                season="fall",
                price_min=895,
                price_max=895,
                inventory=[
                    CatalogAIInventoryProposal(
                        size="M",
                        availability="in stock",
                        inventory_qty=8,
                        objective_weight=0.9,
                    )
                ],
            )
        ],
    )


def test_product_proposal_accepts_declared_color_variants_with_one_shared_design():
    proposal = _proposal()
    proposal = proposal.model_copy(
        update={
            "variants": [
                proposal.variants[0],
                proposal.variants[0].model_copy(update={"color": "Ivory"}),
            ]
        }
    )

    validated = CatalogAIProductProposal.model_validate(proposal.model_dump())

    assert validated.variant_axes == ["color"]
    assert validated.primary_variant_index == 0
    assert validated.design_specification.product_type == "single-breasted coat"


def test_product_proposal_rejects_undeclared_material_drift():
    proposal = _proposal()
    payload = proposal.model_dump()
    payload["variants"].append({**payload["variants"][0], "color": "Ivory", "material": "silk"})

    with pytest.raises(ValidationError, match="material.*declared variant axis"):
        CatalogAIProductProposal.model_validate(payload)


def test_product_proposal_rejects_cross_variant_gender_or_season_drift():
    proposal = _proposal()
    payload = proposal.model_dump()
    payload["variants"].append({**payload["variants"][0], "color": "Ivory", "gender": "men"})

    with pytest.raises(ValidationError, match="gender and season"):
        CatalogAIProductProposal.model_validate(payload)


def _moderation(*, flagged: bool = False, category: str = "violence"):
    return SimpleNamespace(
        type="moderation_result",
        flagged=flagged,
        model="omni-moderation-latest",
        categories={category: flagged},
        category_scores={category: 0.99 if flagged else 0.001},
        category_applied_input_types={category: ["text"]},
    )


def _response(
    proposal: CatalogAIProductProposal | None = None,
    *,
    input_flagged: bool = False,
    output_flagged: bool = False,
    status: str = "completed",
):
    return SimpleNamespace(
        id="resp_catalog_123",
        model="gpt-5.5-2026-05-01",
        status=status,
        output_parsed=proposal,
        moderation=SimpleNamespace(
            input=_moderation(flagged=input_flagged, category="violence"),
            output=_moderation(flagged=output_flagged, category="harassment"),
        ),
        usage=SimpleNamespace(
            model_dump=lambda mode="json": {
                "input_tokens": 120,
                "output_tokens": 80,
                "total_tokens": 200,
                "input_tokens_details": {"cached_tokens": 20},
                "output_tokens_details": {"reasoning_tokens": 10},
            }
        ),
    )


class _FakeResponses:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class _FakeClient:
    def __init__(self, *results):
        self.responses = _FakeResponses(results)


def test_valid_instruction_saves_one_moderated_draft_and_safe_events(db):
    settings = _settings()
    workflow = _run(db, settings)
    client = _FakeClient(_response(_proposal()))
    private_instruction = "Create a black wool coat. Private presenter note: launch code ORCHID."

    result = execute_catalog_ai_command(
        db,
        workflow_id=workflow.id,
        command=CatalogAICommandRequest(
            instruction=private_instruction,
            expected_draft_version=0,
        ),
        idempotency_key="command-create-coat",
        principal=_principal(),
        settings=settings,
        client=client,
    )

    assert result.status == "succeeded"
    assert result.replayed is False
    assert result.draft is not None
    assert result.draft.draft_version == 1
    assert result.draft.product.title == "Midnight Atelier Coat"
    assert result.draft.product.variants[0].inventory[0].store_id == "1001"
    assert result.draft.product.seed_run_id == "run_catalog"
    assert len(db.scalars(select(CatalogDraftRevision)).all()) == 1

    events = db.scalars(
        select(CatalogWorkflowEvent)
        .where(CatalogWorkflowEvent.workflow_id == workflow.id)
        .order_by(CatalogWorkflowEvent.sequence)
    ).all()
    assert [(event.capability, event.status) for event in events] == [
        ("workflow", "started"),
        ("moderation", "succeeded"),
        ("responses", "succeeded"),
    ]
    persisted = json.dumps(
        [
            {
                "business": event.business_summary,
                "request": event.request_json,
                "response": event.response_json,
            }
            for event in events
        ],
        sort_keys=True,
    )
    assert private_instruction not in persisted
    assert "ORCHID" not in persisted

    call = client.responses.calls[0]
    assert call["store"] is False
    assert call["moderation"] == {"model": "omni-moderation-latest"}
    assert call["max_output_tokens"] == 2500
    assert call["text_format"] is CatalogAIProductProposal
    assert call["safety_identifier"] != _principal().provider_user_id
    assert private_instruction in json.dumps(call["input"])


def test_follow_up_refines_same_product_and_increments_draft_version(db):
    settings = _settings()
    workflow = _run(db, settings)
    client = _FakeClient(
        _response(_proposal()),
        _response(_proposal(title="Ivory Atelier Coat", color="Ivory")),
    )
    first = execute_catalog_ai_command(
        db,
        workflow_id=workflow.id,
        command=CatalogAICommandRequest(
            instruction="Create a black wool coat.", expected_draft_version=0
        ),
        idempotency_key="command-create",
        principal=_principal(),
        settings=settings,
        client=client,
    )
    assert first.draft is not None

    refined = execute_catalog_ai_command(
        db,
        workflow_id=workflow.id,
        command=CatalogAICommandRequest(
            instruction="Change the color to ivory.",
            current_draft_id=first.draft.id,
            expected_draft_version=1,
        ),
        idempotency_key="command-refine",
        principal=_principal(),
        settings=settings,
        client=client,
    )

    assert refined.draft is not None
    assert refined.draft.product_id == first.draft.product_id
    assert refined.draft.id != first.draft.id
    assert refined.draft.draft_version == 2
    assert refined.draft.product.title == "Ivory Atelier Coat"
    assert len(db.scalars(select(CatalogDraftRevision)).all()) == 2
    assert db.get(CatalogWorkflow, workflow.id).draft_revision_id == refined.draft.id
    second_input = json.dumps(client.responses.calls[1]["input"])
    assert "Midnight Atelier Coat" in second_input
    assert "Change the color to ivory" in second_input


def test_new_run_cannot_create_a_conflicting_private_product_identity(db):
    settings = _settings()
    first_run = _run(db, settings, idempotency_key="catalog-ai-workflow-1")
    first_client = _FakeClient(_response(_proposal()))
    first = execute_catalog_ai_command(
        db,
        workflow_id=first_run.id,
        command=CatalogAICommandRequest(
            instruction="Create a black wool coat.", expected_draft_version=0
        ),
        idempotency_key="command-first-product",
        principal=_principal(),
        settings=settings,
        client=first_client,
    )
    assert first.draft is not None

    second_run = _run(db, settings, idempotency_key="catalog-ai-workflow-2")
    second_client = _FakeClient(_response(_proposal()))
    with pytest.raises(CatalogAICommandError) as conflict:
        execute_catalog_ai_command(
            db,
            workflow_id=second_run.id,
            command=CatalogAICommandRequest(
                instruction="Create the same black wool coat.", expected_draft_version=0
            ),
            idempotency_key="command-conflicting-product",
            principal=_principal(),
            settings=settings,
            client=second_client,
        )

    assert conflict.value.code == "draft_state_conflict"
    assert conflict.value.status_code == 409
    assert len(db.scalars(select(CatalogDraftRevision)).all()) == 1
    assert db.get(CatalogWorkflow, second_run.id).draft_revision_id is None


@pytest.mark.parametrize("blocked_side", ["input", "output"])
def test_moderation_block_persists_no_draft_or_unsafe_copy(db, blocked_side):
    settings = _settings()
    workflow = _run(db, settings)
    unsafe_copy = "Unsafe generated copy that must never be persisted."
    proposal = _proposal(title=unsafe_copy)
    client = _FakeClient(
        _response(
            proposal,
            input_flagged=blocked_side == "input",
            output_flagged=blocked_side == "output",
        )
    )
    unsafe_instruction = "Unsafe presenter input that must never be persisted."

    result = execute_catalog_ai_command(
        db,
        workflow_id=workflow.id,
        command=CatalogAICommandRequest(
            instruction=unsafe_instruction, expected_draft_version=0
        ),
        idempotency_key=f"command-block-{blocked_side}",
        principal=_principal(),
        settings=settings,
        client=client,
    )

    assert result.status == "blocked"
    assert result.draft is None
    assert db.scalars(select(CatalogDraftRevision)).all() == []
    events = db.scalars(
        select(CatalogWorkflowEvent).where(CatalogWorkflowEvent.workflow_id == workflow.id)
    ).all()
    persisted = json.dumps(
        [
            {
                "business": event.business_summary,
                "request": event.request_json,
                "response": event.response_json,
            }
            for event in events
        ],
        sort_keys=True,
    )
    assert unsafe_instruction not in persisted
    assert unsafe_copy not in persisted
    assert any(event.capability == "moderation" and event.status == "blocked" for event in events)
    assert any(event.capability == "responses" and event.status == "blocked" for event in events)


@pytest.mark.parametrize(
    ("provider_result", "error_code"),
    [
        (_response(None), "invalid_structured_output"),
        (_response(_proposal(), status="incomplete"), "invalid_structured_output"),
        (TimeoutError("provider timeout"), "responses_timeout"),
    ],
)
def test_provider_failures_preserve_prior_draft_and_emit_retryable_event(
    db, provider_result, error_code
):
    settings = _settings()
    workflow = _run(db, settings)
    client = _FakeClient(_response(_proposal()), provider_result)
    first = execute_catalog_ai_command(
        db,
        workflow_id=workflow.id,
        command=CatalogAICommandRequest(
            instruction="Create a black wool coat.", expected_draft_version=0
        ),
        idempotency_key="command-initial",
        principal=_principal(),
        settings=settings,
        client=client,
    )
    assert first.draft is not None

    with pytest.raises(CatalogAICommandError) as failure:
        execute_catalog_ai_command(
            db,
            workflow_id=workflow.id,
            command=CatalogAICommandRequest(
                instruction="Refine the coat.",
                current_draft_id=first.draft.id,
                expected_draft_version=1,
            ),
            idempotency_key=f"command-fail-{error_code}",
            principal=_principal(),
            settings=settings,
            client=client,
        )

    assert failure.value.code == error_code
    assert failure.value.retryable is True
    assert len(db.scalars(select(CatalogDraftRevision)).all()) == 1
    assert db.get(CatalogWorkflow, workflow.id).draft_revision_id == first.draft.id
    failed_event = db.scalar(
        select(CatalogWorkflowEvent).where(
            CatalogWorkflowEvent.workflow_id == workflow.id,
            CatalogWorkflowEvent.status == "failed",
            CatalogWorkflowEvent.error_code == error_code,
        )
    )
    assert failed_event is not None
    assert failed_event.retryable is True


def test_missing_configuration_preserves_prior_state_and_is_replay_safe(db):
    settings = _settings(openai_api_key=None)
    workflow = _run(db, settings)
    command = CatalogAICommandRequest(
        instruction="Create a black wool coat.", expected_draft_version=0
    )

    for _ in range(2):
        with pytest.raises(CatalogAICommandError) as failure:
            execute_catalog_ai_command(
                db,
                workflow_id=workflow.id,
                command=command,
                idempotency_key="command-unconfigured",
                principal=_principal(),
                settings=settings,
            )
        assert failure.value.code == "responses_unavailable"

    assert db.scalars(select(CatalogDraftRevision)).all() == []
    failures = db.scalars(
        select(CatalogWorkflowEvent).where(
            CatalogWorkflowEvent.workflow_id == workflow.id,
            CatalogWorkflowEvent.error_code == "responses_unavailable",
        )
    ).all()
    assert len(failures) == 1


def test_missing_catalog_context_fails_before_provider_call_and_records_event(db):
    settings = _settings()
    workflow = _run(db, settings)
    db.execute(delete(Store))
    db.commit()
    client = _FakeClient(_response(_proposal()))

    with pytest.raises(CatalogAICommandError) as failure:
        execute_catalog_ai_command(
            db,
            workflow_id=workflow.id,
            command=CatalogAICommandRequest(
                instruction="Create a black wool coat.", expected_draft_version=0
            ),
            idempotency_key="command-no-catalog-context",
            principal=_principal(),
            settings=settings,
            client=client,
        )

    assert failure.value.code == "catalog_context_unavailable"
    assert failure.value.retryable is False
    assert client.responses.calls == []
    assert db.scalars(select(CatalogDraftRevision)).all() == []
    failure_event = db.scalar(
        select(CatalogWorkflowEvent).where(
            CatalogWorkflowEvent.workflow_id == workflow.id,
            CatalogWorkflowEvent.error_code == "catalog_context_unavailable",
        )
    )
    assert failure_event is not None


def test_replayed_command_returns_saved_result_without_second_responses_call(db):
    settings = _settings()
    workflow = _run(db, settings)
    client = _FakeClient(_response(_proposal()))
    command = CatalogAICommandRequest(
        instruction="Create a black wool coat.", expected_draft_version=0
    )

    first = execute_catalog_ai_command(
        db,
        workflow_id=workflow.id,
        command=command,
        idempotency_key="command-replay",
        principal=_principal(),
        settings=settings,
        client=client,
    )
    replay = execute_catalog_ai_command(
        db,
        workflow_id=workflow.id,
        command=command,
        idempotency_key="command-replay",
        principal=_principal(),
        settings=settings,
        client=client,
    )
    with pytest.raises(HTTPException) as mismatch:
        execute_catalog_ai_command(
            db,
            workflow_id=workflow.id,
            command=CatalogAICommandRequest(
                instruction="Create a different coat.", expected_draft_version=0
            ),
            idempotency_key="command-replay",
            principal=_principal(),
            settings=settings,
            client=client,
        )

    assert first.draft is not None and replay.draft is not None
    assert replay.replayed is True
    assert replay.draft.id == first.draft.id
    assert len(client.responses.calls) == 1
    assert len(db.scalars(select(CatalogDraftRevision)).all()) == 1
    assert mismatch.value.status_code == 409


def test_developer_projection_contains_metadata_but_not_private_instruction(db):
    settings = _settings()
    principal = _principal()
    workflow = _run(db, settings)
    client = _FakeClient(_response(_proposal()))
    private_instruction = "Create the coat. private_prompt=internal-launch-plan"

    execute_catalog_ai_command(
        db,
        workflow_id=workflow.id,
        command=CatalogAICommandRequest(
            instruction=private_instruction, expected_draft_version=0
        ),
        idempotency_key="command-developer-projection",
        principal=principal,
        settings=settings,
        client=client,
    )
    projection = get_catalog_workflow_projection(
        db, workflow_id=workflow.id, principal=principal, developer=True, settings=settings
    )
    encoded = projection.model_dump_json()

    assert "gpt-5.5-2026-05-01" in encoded
    assert "resp_catalog_123" in encoded
    assert '"input_tokens":120' in encoded
    assert private_instruction not in encoded
    assert "internal-launch-plan" not in encoded
