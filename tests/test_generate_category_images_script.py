from __future__ import annotations

import json

import httpx

from scripts.generate_category_images import fetch_category_ids, poll_image_job, run_category


def test_fetch_category_ids_reads_catalog_api():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/categories"
        return httpx.Response(
            200,
            json={
                "categories": [
                    {"id": "womens_apparel", "label": "Women's Apparel"},
                    {"id": "shoes", "label": "Shoes"},
                ]
            },
        )

    with httpx.Client(base_url="https://products.example", transport=httpx.MockTransport(handler)) as client:
        assert fetch_category_ids(client) == ["womens_apparel", "shoes"]


def test_run_category_batches_until_no_attempted_variants():
    posts: list[dict] = []
    job_payloads = [
        {"id": "imgjob_1", "status": "queued"},
        {"id": "imgjob_2", "status": "queued"},
    ]
    completed_jobs = {
        "imgjob_1": {
            "id": "imgjob_1",
            "status": "succeeded",
            "attempted": 2,
            "generated": 2,
            "skipped": 0,
            "failed_count": 0,
        },
        "imgjob_2": {
            "id": "imgjob_2",
            "status": "succeeded",
            "attempted": 0,
            "generated": 0,
            "skipped": 0,
            "failed_count": 0,
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/admin/product-images/generate":
            posts.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json=job_payloads.pop(0))
        if request.method == "GET" and request.url.path.startswith("/admin/product-images/jobs/"):
            job_id = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json=completed_jobs[job_id])
        return httpx.Response(404)

    with httpx.Client(base_url="https://products.example", transport=httpx.MockTransport(handler)) as client:
        summary = run_category(
            client,
            category="womens_apparel",
            batch_size=2,
            detail_count=3,
            thumbnail_size=320,
            store_id=None,
            overwrite=False,
            poll_interval=0.5,
            job_timeout=5.0,
            max_batches=0,
            continue_on_failure=False,
        )

    assert [post["category"] for post in posts] == ["womens_apparel", "womens_apparel"]
    assert all(post["missing_images_only"] is True for post in posts)
    assert summary.batches == 2
    assert summary.attempted == 2
    assert summary.generated == 2
    assert summary.complete is True


def test_poll_image_job_retries_transient_gateway_errors():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.method == "GET"
        assert request.url.path == "/admin/product-images/jobs/imgjob_1"
        if calls == 1:
            return httpx.Response(502, json={"detail": "Bad Gateway"})
        return httpx.Response(
            200,
            json={
                "id": "imgjob_1",
                "status": "succeeded",
                "attempted": 1,
                "generated": 1,
                "skipped": 0,
                "failed_count": 0,
            },
        )

    with httpx.Client(base_url="https://products.example", transport=httpx.MockTransport(handler)) as client:
        job = poll_image_job(client, job_id="imgjob_1", poll_interval=0.01, job_timeout=5.0)

    assert calls == 2
    assert job.status == "succeeded"
    assert job.generated == 1
