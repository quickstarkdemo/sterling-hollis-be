#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx


TERMINAL_STATUSES = {"succeeded", "failed"}


@dataclass
class CategoryImageJobSummary:
    job_id: str
    status: str
    attempted: int
    generated: int
    skipped: int
    failed_count: int
    error_message: str | None = None


@dataclass
class CategoryImageSummary:
    category: str
    batches: int = 0
    attempted: int = 0
    generated: int = 0
    skipped: int = 0
    failed_count: int = 0
    jobs: list[CategoryImageJobSummary] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return bool(self.jobs and self.jobs[-1].attempted == 0)


def _split_values(raw_values: list[str]) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        for value in raw.replace(";", ",").split(","):
            cleaned = value.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                values.append(cleaned)
    return values


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _request_json(client: httpx.Client, method: str, path: str, **kwargs) -> dict[str, Any]:
    response = client.request(method, path, **kwargs)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"{method} {path} did not return a JSON object.")
    return payload


def fetch_category_ids(client: httpx.Client) -> list[str]:
    payload = _request_json(client, "GET", "/api/categories")
    categories = payload.get("categories")
    if not isinstance(categories, list):
        raise RuntimeError("GET /api/categories did not return a categories list.")
    category_ids = [str(category.get("id") or "").strip() for category in categories if isinstance(category, dict)]
    return [category_id for category_id in category_ids if category_id]


def enqueue_image_job(
    client: httpx.Client,
    *,
    category: str,
    batch_size: int,
    detail_count: int | None,
    thumbnail_size: int | None,
    store_id: str | None,
    overwrite: bool,
) -> str:
    body: dict[str, Any] = {
        "category": category,
        "limit": batch_size,
        "missing_images_only": not overwrite,
        "overwrite": overwrite,
    }
    if detail_count is not None:
        body["detail_count"] = detail_count
    if thumbnail_size is not None:
        body["thumbnail_size"] = thumbnail_size
    if store_id:
        body["store_id"] = store_id

    payload = _request_json(client, "POST", "/admin/product-images/generate", json=body)
    job_id = str(payload.get("id") or "").strip()
    if not job_id:
        raise RuntimeError("POST /admin/product-images/generate did not return a job id.")
    return job_id


def poll_image_job(
    client: httpx.Client,
    *,
    job_id: str,
    poll_interval: float,
    job_timeout: float,
) -> CategoryImageJobSummary:
    deadline = time.monotonic() + job_timeout
    while True:
        payload = _request_json(client, "GET", f"/admin/product-images/jobs/{job_id}")
        status = str(payload.get("status") or "")
        if status in TERMINAL_STATUSES:
            return CategoryImageJobSummary(
                job_id=job_id,
                status=status,
                attempted=int(payload.get("attempted") or 0),
                generated=int(payload.get("generated") or 0),
                skipped=int(payload.get("skipped") or 0),
                failed_count=int(payload.get("failed_count") or 0),
                error_message=payload.get("error_message"),
            )
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Timed out waiting for image generation job {job_id}.")
        time.sleep(poll_interval)


def run_category(
    client: httpx.Client,
    *,
    category: str,
    batch_size: int,
    detail_count: int | None,
    thumbnail_size: int | None,
    store_id: str | None,
    overwrite: bool,
    poll_interval: float,
    job_timeout: float,
    max_batches: int,
    continue_on_failure: bool,
) -> CategoryImageSummary:
    summary = CategoryImageSummary(category=category)
    while max_batches <= 0 or summary.batches < max_batches:
        job_id = enqueue_image_job(
            client,
            category=category,
            batch_size=batch_size,
            detail_count=detail_count,
            thumbnail_size=thumbnail_size,
            store_id=store_id,
            overwrite=overwrite,
        )
        _log(f"{category}: queued {job_id}")
        job = poll_image_job(client, job_id=job_id, poll_interval=poll_interval, job_timeout=job_timeout)
        summary.batches += 1
        summary.attempted += job.attempted
        summary.generated += job.generated
        summary.skipped += job.skipped
        summary.failed_count += job.failed_count
        summary.jobs.append(job)
        _log(
            f"{category}: {job.job_id} {job.status} "
            f"attempted={job.attempted} generated={job.generated} skipped={job.skipped} failed={job.failed_count}"
        )

        if job.status == "failed" and not continue_on_failure:
            raise RuntimeError(f"{category}: image generation job {job.job_id} failed: {job.error_message or 'unknown error'}")
        if job.attempted == 0:
            break
    return summary


def run_all_categories(
    *,
    base_url: str,
    categories: list[str],
    exclude_categories: list[str],
    batch_size: int,
    detail_count: int | None,
    thumbnail_size: int | None,
    store_id: str | None,
    overwrite: bool,
    poll_interval: float,
    job_timeout: float,
    request_timeout: float,
    max_batches_per_category: int,
    continue_on_failure: bool,
    plan_only: bool,
) -> dict[str, Any]:
    with httpx.Client(base_url=base_url.rstrip("/"), timeout=request_timeout) as client:
        available_categories = fetch_category_ids(client)
        selected = categories or available_categories
        excluded = set(exclude_categories)
        selected = [category for category in selected if category not in excluded]
        unknown = sorted(set(selected) - set(available_categories))
        if unknown:
            raise ValueError(f"Unknown categories: {', '.join(unknown)}")

        if plan_only:
            return {
                "base_url": base_url.rstrip("/"),
                "plan_only": True,
                "categories": selected,
                "batch_size": batch_size,
                "store_id": store_id,
                "overwrite": overwrite,
            }

        summaries = [
            run_category(
                client,
                category=category,
                batch_size=batch_size,
                detail_count=detail_count,
                thumbnail_size=thumbnail_size,
                store_id=store_id,
                overwrite=overwrite,
                poll_interval=poll_interval,
                job_timeout=job_timeout,
                max_batches=max_batches_per_category,
                continue_on_failure=continue_on_failure,
            )
            for category in selected
        ]

    return {
        "base_url": base_url.rstrip("/"),
        "categories": [asdict(summary) | {"complete": summary.complete} for summary in summaries],
        "totals": {
            "categories": len(summaries),
            "batches": sum(summary.batches for summary in summaries),
            "attempted": sum(summary.attempted for summary in summaries),
            "generated": sum(summary.generated for summary in summaries),
            "skipped": sum(summary.skipped for summary in summaries),
            "failed_count": sum(summary.failed_count for summary in summaries),
            "complete_categories": sum(1 for summary in summaries if summary.complete),
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate product images by orchestrating API image jobs per category.")
    parser.add_argument("--base-url", default="http://localhost:8000", help="FastAPI base URL.")
    parser.add_argument("--category", action="append", default=[], help="Category id to process. Repeat or comma-separate.")
    parser.add_argument("--exclude-category", action="append", default=[], help="Category id to skip. Repeat or comma-separate.")
    parser.add_argument("--batch-size", type=int, default=50, help="Variants per API job. API max is 500.")
    parser.add_argument("--detail-count", type=int, help="Number of full-size detail images per variant.")
    parser.add_argument("--thumbnail-size", type=int, help="Maximum thumbnail width/height in pixels.")
    parser.add_argument("--store-id", help="Only generate variants stocked by this store.")
    parser.add_argument("--overwrite", action="store_true", help="Regenerate existing images.")
    parser.add_argument("--poll-interval", type=float, default=5.0, help="Seconds between job status checks.")
    parser.add_argument("--job-timeout", type=float, default=3600.0, help="Maximum seconds to wait for one API job.")
    parser.add_argument("--request-timeout", type=float, default=30.0, help="HTTP request timeout in seconds.")
    parser.add_argument("--max-batches-per-category", type=int, default=0, help="0 means continue until no variants remain.")
    parser.add_argument("--continue-on-failure", action="store_true", help="Continue to later categories if one job fails.")
    parser.add_argument("--plan-only", action="store_true", help="Fetch categories and print the planned work without enqueueing jobs.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    summary = run_all_categories(
        base_url=args.base_url,
        categories=_split_values(args.category),
        exclude_categories=_split_values(args.exclude_category),
        batch_size=max(1, min(args.batch_size, 500)),
        detail_count=args.detail_count,
        thumbnail_size=args.thumbnail_size,
        store_id=args.store_id,
        overwrite=args.overwrite,
        poll_interval=max(0.5, args.poll_interval),
        job_timeout=max(1.0, args.job_timeout),
        request_timeout=max(1.0, args.request_timeout),
        max_batches_per_category=args.max_batches_per_category,
        continue_on_failure=args.continue_on_failure,
        plan_only=args.plan_only,
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
