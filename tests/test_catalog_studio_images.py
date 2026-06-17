from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from io import BytesIO
from types import SimpleNamespace

from PIL import Image
from sqlalchemy import select

from app.catalog.admin_schemas import ProductDraft
from app.config import get_settings
from app.models import (
    CatalogDraftRevision,
    CatalogWorkflow,
    CatalogWorkflowEvent,
    ImageGenerationJob,
    IndexJob,
    ProductVariant,
)
from app.services.catalog_images import process_catalog_image_job
from app.services.image_jobs import (
    claim_next_image_generation_job,
    process_image_generation_job,
    recover_stale_image_generation_jobs,
)
from tests.test_admin_catalog_api import _admin_catalog_client, _headers, _snapshot


def _jpeg_base64(color: str = "navy") -> str:
    buffer = BytesIO()
    Image.new("RGB", (24, 24), color=color).save(buffer, format="JPEG")
    return base64.b64encode(buffer.getvalue()).decode()


class FakeImages:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.generate_calls: list[dict] = []
        self.edit_calls: list[dict] = []

    def _response(self):
        if self.error:
            raise self.error
        return SimpleNamespace(
            data=[SimpleNamespace(b64_json=_jpeg_base64())],
            _request_id="req_catalog_image_1",
            usage=SimpleNamespace(
                model_dump=lambda **_kwargs: {
                    "input_tokens": 12,
                    "output_tokens": 100,
                    "total_tokens": 112,
                }
            ),
        )

    def generate(self, **kwargs):
        self.generate_calls.append(kwargs)
        return self._response()

    def edit(self, **kwargs):
        self.edit_calls.append(kwargs)
        return self._response()


def _draft_and_workflow(client):
    payload = _snapshot(title="Image Workflow Coat")
    payload["product"]["metadata"]["image_direction"] = "Soft shadow, quiet luxury."
    payload["product"]["variants"][0]["image_link"] = None
    payload["product"]["variants"][0]["image_set"] = {}
    draft_response = client.post(
        "/api/admin/catalog/products/drafts",
        json=payload,
        headers=_headers("image-draft"),
    )
    assert draft_response.status_code == 201
    draft = draft_response.json()
    workflow_response = client.post(
        "/api/admin/catalog/workflows",
        json={
            "title": "Catalog image workflow",
            "business_summary": "Create and approve the product imagery.",
            "draft_id": draft["id"],
        },
        headers=_headers("image-workflow"),
    )
    assert workflow_response.status_code == 201
    return draft, workflow_response.json()


def _variant_family_draft_and_workflow(client):
    payload = _snapshot(title="Coherent Image Family Coat")
    product = payload["product"]
    product["design_specification"] = {
        "product_type": "single-breasted coat",
        "silhouette": "long sculpted column",
        "construction": "notched collar with concealed front closure",
        "distinguishing_features": ["curved shoulder seam", "welt pockets"],
    }
    product["variant_axes"] = ["color"]
    product["primary_variant_index"] = 0
    primary = product["variants"][0]
    primary["image_link"] = None
    primary["image_set"] = {}
    product["variants"] = [
        primary,
        {**primary, "color": "Ivory", "image_link": None, "image_set": {}},
        {**primary, "color": "Burgundy", "image_link": None, "image_set": {}},
    ]
    draft_response = client.post(
        "/api/admin/catalog/products/drafts",
        json=payload,
        headers=_headers("variant-family-draft"),
    )
    assert draft_response.status_code == 201
    draft = draft_response.json()
    workflow_response = client.post(
        "/api/admin/catalog/workflows",
        json={
            "title": "Coherent image family workflow",
            "business_summary": "Generate one approved design across color variants.",
            "draft_id": draft["id"],
        },
        headers=_headers("variant-family-workflow"),
    )
    assert workflow_response.status_code == 201
    return draft, workflow_response.json()


def _enqueue_variant_set(client, workflow_id: str, draft_id: str, key: str):
    return client.post(
        f"/api/admin/catalog/workflows/{workflow_id}/image-variant-sets",
        json={"draft_id": draft_id, "expected_draft_version": 1},
        headers=_headers(key),
    )


def _enqueue(client, workflow_id: str, draft_id: str, key: str, **overrides):
    payload = {
        "action": "generate",
        "draft_id": draft_id,
        "expected_draft_version": 1,
        "variant_index": 0,
        **overrides,
    }
    return client.post(
        f"/api/admin/catalog/workflows/{workflow_id}/image-commands",
        json=payload,
        headers=_headers(key),
    )


def _process(sessions, fake_images: FakeImages):
    with sessions() as db:
        job = claim_next_image_generation_job(db)
        assert job is not None
        return process_catalog_image_job(
            db,
            job=job,
            settings=get_settings(),
            client=SimpleNamespace(images=fake_images),
        )


def test_generate_approve_publish_and_index_catalog_image(monkeypatch, tmp_path):
    monkeypatch.setenv("PRODUCT_IMAGE_OUTPUT_DIR", str(tmp_path / "images"))
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://catalog.example")
    with _admin_catalog_client(monkeypatch) as (client, sessions):
        draft, workflow = _draft_and_workflow(client)
        queued = _enqueue(client, workflow["id"], draft["id"], "image-generate")
        assert queued.status_code == 202
        job = queued.json()
        replay = _enqueue(client, workflow["id"], draft["id"], "image-generate")
        assert replay.status_code == 202
        assert replay.json()["id"] == job["id"]

        result = _process(sessions, FakeImages())
        assert result.status == "succeeded"

        detail = client.get(
            f"/api/admin/catalog/workflows/{workflow['id']}/image-jobs/{job['id']}"
        )
        assert detail.status_code == 200
        assert detail.json()["status"] == "succeeded"
        with sessions() as db:
            revision = db.get(CatalogDraftRevision, draft["id"])
            image_set = revision.snapshot_json["variants"][0]["image_set"]
            assert image_set["approval_status"] == "review"
            assert image_set["source"] == "catalog_studio"
            assert image_set["generated_by"] == "gpt-image-2"
            event = db.scalar(
                select(CatalogWorkflowEvent).where(
                    CatalogWorkflowEvent.workflow_id == workflow["id"],
                    CatalogWorkflowEvent.capability == "image_generation",
                    CatalogWorkflowEvent.status == "succeeded",
                )
            )
            assert event.request_id == "req_catalog_image_1"
            assert event.usage_json["total_tokens"] == 112
            assert event.response_json["approval_status"] == "review"
            assert event.response_json["image_url"].startswith("https://catalog.example/")
            queued_event = db.scalar(
                select(CatalogWorkflowEvent).where(
                    CatalogWorkflowEvent.workflow_id == workflow["id"],
                    CatalogWorkflowEvent.capability == "image_generation",
                    CatalogWorkflowEvent.status == "queued",
                )
            )
            assert queued_event.request_json == {
                "action": "generate",
                "draft_id": draft["id"],
                "draft_version": 1,
                "variant_index": 0,
            }

        blocked = client.post(
            f"/api/admin/catalog/products/{draft['product_id']}/publish",
            json={"draft_id": draft["id"], "expected_version": 0},
            headers=_headers("publish-before-image-approval"),
        )
        assert blocked.status_code == 409
        assert "require approval" in blocked.json()["detail"]

        approved = client.post(
            f"/api/admin/catalog/workflows/{workflow['id']}/image-jobs/{job['id']}/approve",
            json={"draft_id": draft["id"], "expected_draft_version": 1},
        )
        assert approved.status_code == 200
        assert approved.json()["approval_status"] == "approved"
        approval_replay = client.post(
            f"/api/admin/catalog/workflows/{workflow['id']}/image-jobs/{job['id']}/approve",
            json={"draft_id": draft["id"], "expected_draft_version": 1},
        )
        assert approval_replay.status_code == 200
        published = client.post(
            f"/api/admin/catalog/products/{draft['product_id']}/publish",
            json={"draft_id": draft["id"], "expected_version": 0},
            headers=_headers("publish-after-image-approval"),
        )
        assert published.status_code == 200
        with sessions() as db:
            index_job = db.scalar(select(IndexJob).where(IndexJob.run_id == "run_catalog"))
            assert index_job is not None
            assert index_job.status == "queued"
            variant = db.scalar(
                select(ProductVariant).where(
                    ProductVariant.catalog_product_id == draft["product_id"]
                )
            )
            assert "file_path" not in variant.image_set
            assert "history" not in variant.image_set


def test_refine_uses_approved_source_and_preserves_history(monkeypatch, tmp_path):
    monkeypatch.setenv("PRODUCT_IMAGE_OUTPUT_DIR", str(tmp_path / "images"))
    with _admin_catalog_client(monkeypatch) as (client, sessions):
        draft, workflow = _draft_and_workflow(client)
        first = _enqueue(client, workflow["id"], draft["id"], "first-image").json()
        _process(sessions, FakeImages())
        approved = client.post(
            f"/api/admin/catalog/workflows/{workflow['id']}/image-jobs/{first['id']}/approve",
            json={"draft_id": draft["id"], "expected_draft_version": 1},
        )
        assert approved.status_code == 200

        refine = _enqueue(
            client,
            workflow["id"],
            draft["id"],
            "refine-image",
            action="refine",
            refinement_prompt="Make the background warmer.",
        )
        assert refine.status_code == 202
        images = FakeImages()
        _process(sessions, images)
        assert len(images.edit_calls) == 1
        assert not images.generate_calls
        with sessions() as db:
            revision = db.get(CatalogDraftRevision, draft["id"])
            image_set = revision.snapshot_json["variants"][0]["image_set"]
            assert image_set["job_id"] == refine.json()["id"]
            assert image_set["approval_status"] == "review"
            assert image_set["history"][-1]["job_id"] == first["id"]
            assert image_set["history"][-1]["approval_status"] == "approved"


def test_variant_set_requires_approved_primary_and_enqueues_edit_children(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("PRODUCT_IMAGE_OUTPUT_DIR", str(tmp_path / "images"))
    with _admin_catalog_client(monkeypatch) as (client, sessions):
        draft, workflow = _variant_family_draft_and_workflow(client)

        blocked = _enqueue_variant_set(
            client, workflow["id"], draft["id"], "family-before-primary"
        )
        assert blocked.status_code == 409
        assert "approved primary" in blocked.json()["detail"].lower()

        primary = _enqueue(client, workflow["id"], draft["id"], "family-primary").json()
        _process(sessions, FakeImages())
        approved = client.post(
            f"/api/admin/catalog/workflows/{workflow['id']}/image-jobs/{primary['id']}/approve",
            json={"draft_id": draft["id"], "expected_draft_version": 1},
        )
        assert approved.status_code == 200

        queued = _enqueue_variant_set(client, workflow["id"], draft["id"], "family-children")
        assert queued.status_code == 202
        family = queued.json()
        assert family["status"] == "queued"
        assert len(family["jobs"]) == 2
        assert {job["variant_index"] for job in family["jobs"]} == {1, 2}
        assert all(job["action"] == "refine" for job in family["jobs"])
        assert len({job["image_variant_set_id"] for job in family["jobs"]}) == 1

        images = FakeImages()
        _process(sessions, images)
        _process(sessions, images)
        assert len(images.edit_calls) == 2
        prompts = [call["prompt"] for call in images.edit_calls]
        assert any("Ivory" in prompt for prompt in prompts)
        assert any("Burgundy" in prompt for prompt in prompts)
        assert all("Product type: single-breasted coat" in prompt for prompt in prompts)
        assert all("Silhouette: long sculpted column" in prompt for prompt in prompts)

        detail = client.get(
            f"/api/admin/catalog/workflows/{workflow['id']}/image-variant-sets/{family['id']}"
        )
        assert detail.status_code == 200
        assert detail.json()["status"] == "review"

        for child in family["jobs"]:
            approval = client.post(
                f"/api/admin/catalog/workflows/{workflow['id']}/image-jobs/{child['id']}/approve",
                json={"draft_id": draft["id"], "expected_draft_version": 1},
            )
            assert approval.status_code == 200
        complete = client.get(
            f"/api/admin/catalog/workflows/{workflow['id']}/image-variant-sets/{family['id']}"
        )
        assert complete.json()["status"] == "complete"
        with sessions() as db:
            revision = db.get(CatalogDraftRevision, draft["id"])
            snapshot = ProductDraft.model_validate(revision.snapshot_json)
            snapshot.variants[0].image_set["approval_status"] = "review"
            revision.snapshot_json = snapshot.model_dump(mode="json")
            db.commit()
        primary_review = client.get(
            f"/api/admin/catalog/workflows/{workflow['id']}/image-variant-sets/{family['id']}"
        )
        assert primary_review.json()["status"] == "review"
        with sessions() as db:
            revision = db.get(CatalogDraftRevision, draft["id"])
            snapshot = ProductDraft.model_validate(revision.snapshot_json)
            snapshot.variants[0].image_set["approval_status"] = "approved"
            revision.snapshot_json = snapshot.model_dump(mode="json")
            db.commit()
        published = client.post(
            f"/api/admin/catalog/products/{draft['product_id']}/publish",
            json={"draft_id": draft["id"], "expected_version": 0},
            headers=_headers("publish-complete-family"),
        )
        assert published.status_code == 200


def test_variant_set_retries_only_failed_children(monkeypatch, tmp_path):
    monkeypatch.setenv("PRODUCT_IMAGE_OUTPUT_DIR", str(tmp_path / "images"))
    with _admin_catalog_client(monkeypatch) as (client, sessions):
        draft, workflow = _variant_family_draft_and_workflow(client)
        primary = _enqueue(client, workflow["id"], draft["id"], "retry-primary").json()
        _process(sessions, FakeImages())
        client.post(
            f"/api/admin/catalog/workflows/{workflow['id']}/image-jobs/{primary['id']}/approve",
            json={"draft_id": draft["id"], "expected_draft_version": 1},
        )
        family = _enqueue_variant_set(
            client, workflow["id"], draft["id"], "retry-family"
        ).json()
        _process(sessions, FakeImages())
        _process(sessions, FakeImages(error=TimeoutError("timed out")))

        partial = client.get(
            f"/api/admin/catalog/workflows/{workflow['id']}/image-variant-sets/{family['id']}"
        ).json()
        assert partial["status"] == "partially_failed"

        replay = _enqueue_variant_set(
            client, workflow["id"], draft["id"], "retry-family"
        )
        assert replay.status_code == 202
        assert replay.json()["status"] == "partially_failed"

        retried = _enqueue_variant_set(
            client, workflow["id"], draft["id"], "retry-family-again"
        )
        assert retried.status_code == 202
        jobs = retried.json()["jobs"]
        assert len(jobs) == 2
        assert sum(job["status"] == "queued" for job in jobs) == 1
        with sessions() as db:
            all_children = db.scalars(
                select(ImageGenerationJob).where(
                    ImageGenerationJob.image_variant_set_id == family["id"]
                )
            ).all()
            assert len(all_children) == 3


def test_late_variant_set_child_is_discarded_after_draft_revision(monkeypatch, tmp_path):
    monkeypatch.setenv("PRODUCT_IMAGE_OUTPUT_DIR", str(tmp_path / "images"))
    with _admin_catalog_client(monkeypatch) as (client, sessions):
        draft, workflow = _variant_family_draft_and_workflow(client)
        primary = _enqueue(client, workflow["id"], draft["id"], "stale-family-primary").json()
        _process(sessions, FakeImages())
        client.post(
            f"/api/admin/catalog/workflows/{workflow['id']}/image-jobs/{primary['id']}/approve",
            json={"draft_id": draft["id"], "expected_draft_version": 1},
        )
        family = _enqueue_variant_set(
            client, workflow["id"], draft["id"], "stale-family-children"
        ).json()
        with sessions() as db:
            original = db.get(CatalogDraftRevision, draft["id"])
            replacement = CatalogDraftRevision(
                id="draft_family_replacement",
                catalog_product_id=original.catalog_product_id,
                base_version=original.base_version,
                status="draft",
                moderation_state="approved",
                snapshot_json=original.snapshot_json,
                created_by=original.created_by,
            )
            db.add(replacement)
            workflow_row = db.get(CatalogWorkflow, workflow["id"])
            workflow_row.draft_revision_id = replacement.id
            db.commit()

        images = FakeImages()
        result = _process(sessions, images)
        assert result.status == "failed"
        assert result.image_variant_set_id == family["id"]
        assert not images.edit_calls
        with sessions() as db:
            original = db.get(CatalogDraftRevision, draft["id"])
            assert original.snapshot_json["variants"][1]["image_set"] == {}


def test_late_image_result_is_discarded_when_workflow_draft_changes(monkeypatch, tmp_path):
    monkeypatch.setenv("PRODUCT_IMAGE_OUTPUT_DIR", str(tmp_path / "images"))
    with _admin_catalog_client(monkeypatch) as (client, sessions):
        draft, workflow = _draft_and_workflow(client)
        queued = _enqueue(client, workflow["id"], draft["id"], "stale-image").json()
        with sessions() as db:
            original = db.get(CatalogDraftRevision, draft["id"])
            replacement = CatalogDraftRevision(
                id="draft_replacement",
                catalog_product_id=original.catalog_product_id,
                base_version=original.base_version,
                status="draft",
                moderation_state="approved",
                snapshot_json=original.snapshot_json,
                created_by=original.created_by,
            )
            db.add(replacement)
            workflow_row = db.get(CatalogWorkflow, workflow["id"])
            workflow_row.draft_revision_id = replacement.id
            db.commit()

        replay = _enqueue(client, workflow["id"], draft["id"], "stale-image")
        assert replay.status_code == 202
        assert replay.json()["id"] == queued["id"]
        images = FakeImages()
        result = _process(sessions, images)
        assert result.status == "failed"
        assert "discarded" in result.error_message
        assert not images.generate_calls
        assert not list((tmp_path / "images").glob(f"{queued['id']}*"))
        with sessions() as db:
            original = db.get(CatalogDraftRevision, draft["id"])
            assert original.snapshot_json["variants"][0]["image_set"] == {}
            stale_event = db.scalar(
                select(CatalogWorkflowEvent).where(
                    CatalogWorkflowEvent.workflow_id == workflow["id"],
                    CatalogWorkflowEvent.error_code == "stale_draft",
                )
            )
            assert stale_event.retryable is True


def test_image_command_idempotency_conflict_and_retryable_provider_failure(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("PRODUCT_IMAGE_OUTPUT_DIR", str(tmp_path / "images"))

    class RateLimitError(RuntimeError):
        status_code = 429
        code = "rate_limit_exceeded"

    with _admin_catalog_client(monkeypatch) as (client, sessions):
        draft, workflow = _draft_and_workflow(client)
        queued = _enqueue(client, workflow["id"], draft["id"], "same-key")
        assert queued.status_code == 202
        conflict = _enqueue(
            client,
            workflow["id"],
            draft["id"],
            "same-key",
            action="refine",
            refinement_prompt="Use a warmer background.",
        )
        assert conflict.status_code == 409

        result = _process(sessions, FakeImages(error=RateLimitError("try later")))
        assert result.status == "failed"
        with sessions() as db:
            failed_event = db.scalar(
                select(CatalogWorkflowEvent).where(
                    CatalogWorkflowEvent.workflow_id == workflow["id"],
                    CatalogWorkflowEvent.error_code == "rate_limit_exceeded",
                )
            )
            assert failed_event.error_code == "rate_limit_exceeded"
            assert failed_event.retryable is True

        retry = _enqueue(client, workflow["id"], draft["id"], "timeout-retry")
        assert retry.status_code == 202
        timeout_result = _process(sessions, FakeImages(error=TimeoutError("timed out")))
        assert timeout_result.status == "failed"
        with sessions() as db:
            timeout_event = db.scalar(
                select(CatalogWorkflowEvent).where(
                    CatalogWorkflowEvent.workflow_id == workflow["id"],
                    CatalogWorkflowEvent.error_code == "TimeoutError",
                )
            )
            assert timeout_event.retryable is True


def test_orphaned_catalog_job_fails_without_entering_legacy_batch_path(monkeypatch, tmp_path):
    monkeypatch.setenv("PRODUCT_IMAGE_OUTPUT_DIR", str(tmp_path / "images"))
    with _admin_catalog_client(monkeypatch) as (client, sessions):
        draft, workflow = _draft_and_workflow(client)
        queued = _enqueue(client, workflow["id"], draft["id"], "orphaned-image").json()
        with sessions() as db:
            job = db.get(ImageGenerationJob, queued["id"])
            job.workflow_id = None
            db.commit()
        with sessions() as db:
            claimed = claim_next_image_generation_job(db)
            assert claimed.id == queued["id"]

        result = process_image_generation_job(sessions, queued["id"])

        assert result.status.value == "failed"
        assert result.attempted == 1
        assert "workflow no longer exists" in result.error_message.lower()


def test_stale_worker_recovery_records_retryable_workflow_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("PRODUCT_IMAGE_OUTPUT_DIR", str(tmp_path / "images"))
    with _admin_catalog_client(monkeypatch) as (client, sessions):
        draft, workflow = _draft_and_workflow(client)
        queued = _enqueue(client, workflow["id"], draft["id"], "worker-stale").json()
        now = datetime(2026, 6, 17, 12, tzinfo=timezone.utc)
        with sessions() as db:
            job = claim_next_image_generation_job(db)
            job.last_heartbeat_at = now - timedelta(minutes=30)
            db.commit()
            recovered = recover_stale_image_generation_jobs(
                db,
                stale_after_seconds=60,
                now=now,
            )
            event = db.scalar(
                select(CatalogWorkflowEvent).where(
                    CatalogWorkflowEvent.workflow_id == workflow["id"],
                    CatalogWorkflowEvent.error_code == "image_worker_stale",
                )
            )
            recovered_job = db.get(ImageGenerationJob, queued["id"])

        assert recovered == 1
        assert recovered_job.status == "failed"
        assert event.retryable is True
