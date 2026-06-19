from __future__ import annotations

import importlib.util
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import create_engine, inspect, text

import app.services.catalog_sources as catalog_sources
from app.catalog.admin_schemas import ProductDraftV2
from app.models import CatalogDraftRevision, ImageGenerationJob
from app.services.auth.admin import require_catalog_admin
from app.services.auth.clerk import AuthenticatedPrincipal
from tests.test_admin_catalog_api import _admin_catalog_client, _headers, _snapshot_v2


def _image_bytes(
    *,
    image_format: str = "JPEG",
    size: tuple[int, int] = (32, 24),
    color: str = "navy",
) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, color=color).save(buffer, format=image_format)
    return buffer.getvalue()


def _upload_bundle(client, files, **form_fields):
    return client.post(
        "/api/admin/catalog/source-bundles",
        data={"title": "Fall supplier handoff", **form_fields},
        files=[("files", file) for file in files],
    )


def test_upload_list_detail_and_private_previews_are_owner_scoped(monkeypatch, tmp_path):
    source_dir = tmp_path / "private-sources"
    monkeypatch.setenv("CATALOG_SOURCE_OUTPUT_DIR", str(source_dir))
    with _admin_catalog_client(monkeypatch) as (client, _sessions):
        draft_response = client.post(
            "/api/admin/catalog/v2/products/drafts",
            json=_snapshot_v2(title="Owner-scoped Source Coat"),
            headers=_headers("owner-source-draft"),
        )
        assert draft_response.status_code == 201
        draft = draft_response.json()
        response = _upload_bundle(
            client,
            [
                ("front.jpg", _image_bytes(color="navy"), "image/jpeg"),
                ("detail.png", _image_bytes(image_format="PNG", color="white"), "image/png"),
            ],
        )

        assert response.status_code == 201, response.text
        assert response.headers["cache-control"] == "private, no-store"
        bundle = response.json()
        assert bundle["title"] == "Fall supplier handoff"
        assert bundle["status"] == "active"
        assert [asset["display_order"] for asset in bundle["assets"]] == [0, 1]
        assert [asset["original_filename"] for asset in bundle["assets"]] == [
            "front.jpg",
            "detail.png",
        ]
        assert all(asset["storage_provider"] == "local_private" for asset in bundle["assets"])
        assert all(asset["preview_url"].startswith("/api/admin/catalog/source-bundles/") for asset in bundle["assets"])
        serialized = response.text.lower()
        assert "storage_key" not in serialized
        assert str(source_dir).lower() not in serialized

        listing = client.get("/api/admin/catalog/source-bundles")
        assert listing.status_code == 200
        assert [item["id"] for item in listing.json()["items"]] == [bundle["id"]]
        detail = client.get(f"/api/admin/catalog/source-bundles/{bundle['id']}")
        assert detail.status_code == 200
        assert detail.json() == bundle

        preview = client.get(bundle["assets"][0]["preview_url"])
        assert preview.status_code == 200
        assert preview.headers["content-type"] == "image/jpeg"
        assert preview.headers["cache-control"] == "private, no-store"
        assert preview.headers["x-content-type-options"] == "nosniff"
        with Image.open(BytesIO(preview.content)) as image:
            assert max(image.size) <= 320

        client.app.dependency_overrides[require_catalog_admin] = lambda: AuthenticatedPrincipal(
            provider="clerk",
            provider_user_id="other_admin",
            email="other@example.com",
            claims={},
        )
        assert client.get(f"/api/admin/catalog/source-bundles/{bundle['id']}").status_code == 404
        assert client.get(bundle["assets"][0]["preview_url"]).status_code == 404
        assert client.delete(
            f"/api/admin/catalog/source-bundles/{bundle['id']}/assets/{bundle['assets'][0]['id']}"
        ).status_code == 404
        assert client.post(
            f"/api/admin/catalog/source-bundles/{bundle['id']}/assets/"
            f"{bundle['assets'][0]['id']}/promote",
            json={"draft_id": draft["id"], "expected_draft_version": 1},
            headers=_headers("cross-owner-promote"),
        ).status_code == 404
        assert _upload_bundle(
            client,
            [("other.jpg", _image_bytes(), "image/jpeg")],
            draft_revision_id=draft["id"],
        ).status_code == 404


def test_upload_rejects_invalid_or_dangerous_images_before_persistence(monkeypatch, tmp_path):
    cases = [
        (
            {},
            ("spoofed.jpg", _image_bytes(image_format="PNG"), "image/jpeg"),
            400,
        ),
        ({}, ("unsupported.gif", _image_bytes(image_format="GIF"), "image/gif"), 415),
        ({}, ("empty.jpg", b"", "image/jpeg"), 400),
        (
            {"CATALOG_SOURCE_UPLOAD_MAX_BYTES": "100"},
            ("large.jpg", _image_bytes(size=(128, 128)), "image/jpeg"),
            413,
        ),
        (
            {"CATALOG_SOURCE_MAX_DIMENSION": "16"},
            ("wide.jpg", _image_bytes(size=(32, 8)), "image/jpeg"),
            400,
        ),
        (
            {"CATALOG_SOURCE_MAX_PIXELS": "100"},
            ("many-pixels.jpg", _image_bytes(size=(16, 16)), "image/jpeg"),
            400,
        ),
        ({}, ("../escape.jpg", _image_bytes(), "image/jpeg"), 400),
    ]

    for index, (settings, file_payload, expected_status) in enumerate(cases):
        source_dir = tmp_path / f"case-{index}"
        monkeypatch.setenv("CATALOG_SOURCE_OUTPUT_DIR", str(source_dir))
        for name in (
            "CATALOG_SOURCE_UPLOAD_MAX_BYTES",
            "CATALOG_SOURCE_MAX_DIMENSION",
            "CATALOG_SOURCE_MAX_PIXELS",
        ):
            monkeypatch.delenv(name, raising=False)
        for name, value in settings.items():
            monkeypatch.setenv(name, value)

        with _admin_catalog_client(monkeypatch) as (client, _sessions):
            response = _upload_bundle(client, [file_payload])
            assert response.status_code == expected_status, (file_payload[0], response.text)
            listing = client.get("/api/admin/catalog/source-bundles")
            assert listing.status_code == 200
            assert listing.json()["items"] == []

        assert not source_dir.exists() or not any(source_dir.rglob("*"))

    bundle_limit_dir = tmp_path / "bundle-limit"
    monkeypatch.setenv("CATALOG_SOURCE_OUTPUT_DIR", str(bundle_limit_dir))
    monkeypatch.setenv("CATALOG_SOURCE_MAX_ASSETS_PER_BUNDLE", "1")
    with _admin_catalog_client(monkeypatch) as (client, _sessions):
        response = _upload_bundle(
            client,
            [
                ("front.jpg", _image_bytes(), "image/jpeg"),
                ("back.jpg", _image_bytes(), "image/jpeg"),
            ],
        )
        assert response.status_code == 422
        assert client.get("/api/admin/catalog/source-bundles").json()["items"] == []
    assert not bundle_limit_dir.exists() or not any(bundle_limit_dir.rglob("*"))

    bomb_dir = tmp_path / "decompression-bomb"
    monkeypatch.setenv("CATALOG_SOURCE_OUTPUT_DIR", str(bomb_dir))
    monkeypatch.setenv("CATALOG_SOURCE_MAX_PIXELS", "100000")
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
    with _admin_catalog_client(monkeypatch) as (client, _sessions):
        response = _upload_bundle(
            client,
            [("bomb.jpg", _image_bytes(size=(16, 16)), "image/jpeg")],
        )
        assert response.status_code == 400
        assert "safe pixel limit" in response.json()["detail"].lower()
        assert client.get("/api/admin/catalog/source-bundles").json()["items"] == []
    assert not bomb_dir.exists() or not any(bomb_dir.rglob("*"))


def test_removal_deletes_unattached_files_and_blocks_active_image_jobs(monkeypatch, tmp_path):
    source_dir = tmp_path / "private-sources"
    monkeypatch.setenv("CATALOG_SOURCE_OUTPUT_DIR", str(source_dir))
    with _admin_catalog_client(monkeypatch) as (client, sessions):
        bundle = _upload_bundle(
            client,
            [
                ("active.jpg", _image_bytes(color="navy"), "image/jpeg"),
                ("unused.jpg", _image_bytes(color="white"), "image/jpeg"),
            ],
        ).json()
        active_asset, unused_asset = bundle["assets"]
        active_original = next(
            path
            for path in source_dir.rglob(f"{active_asset['id']}.*")
            if "preview" not in path.name
        )
        unused_paths = list(source_dir.rglob(f"{unused_asset['id']}*"))
        unused_original = next(path for path in unused_paths if "preview" not in path.name)
        assert len(unused_paths) == 2

        with sessions() as db:
            db.add(
                ImageGenerationJob(
                    id="img_active_source",
                    source_image_path=str(active_original.resolve()),
                    model="gpt-image-2",
                    size="1024x1024",
                    quality="medium",
                    output_format="jpeg",
                    status="queued",
                )
            )
            db.commit()

        blocked = client.delete(
            f"/api/admin/catalog/source-bundles/{bundle['id']}/assets/{active_asset['id']}"
        )
        assert blocked.status_code == 409
        assert "active image job" in blocked.json()["detail"].lower()
        assert active_original.exists()

        original_unlink = Path.unlink

        def deny_unused_removal(path, *, missing_ok=False):
            if path == unused_original:
                raise PermissionError("supplier storage is read-only")
            return original_unlink(path, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", deny_unused_removal)
        cleanup_failed = client.delete(
            f"/api/admin/catalog/source-bundles/{bundle['id']}/assets/{unused_asset['id']}"
        )
        assert cleanup_failed.status_code == 503
        assert all(path.exists() for path in unused_paths)
        assert len(client.get(f"/api/admin/catalog/source-bundles/{bundle['id']}").json()["assets"]) == 2
        monkeypatch.setattr(Path, "unlink", original_unlink)

        removed = client.delete(
            f"/api/admin/catalog/source-bundles/{bundle['id']}/assets/{unused_asset['id']}"
        )
        assert removed.status_code == 204
        assert all(not path.exists() for path in unused_paths)
        detail = client.get(f"/api/admin/catalog/source-bundles/{bundle['id']}").json()
        assert [asset["id"] for asset in detail["assets"]] == [active_asset["id"]]

        with sessions() as db:
            job = db.get(ImageGenerationJob, "img_active_source")
            job.status = "succeeded"
            db.commit()
        assert client.delete(
            f"/api/admin/catalog/source-bundles/{bundle['id']}/assets/{active_asset['id']}"
        ).status_code == 204


def test_promote_source_adds_approved_media_without_changing_inventory(monkeypatch, tmp_path):
    source_dir = tmp_path / "private-sources"
    product_dir = tmp_path / "product-images"
    monkeypatch.setenv("CATALOG_SOURCE_OUTPUT_DIR", str(source_dir))
    monkeypatch.setenv("PRODUCT_IMAGE_OUTPUT_DIR", str(product_dir))
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://catalog.example")

    with _admin_catalog_client(monkeypatch) as (client, sessions):
        draft_response = client.post(
            "/api/admin/catalog/v2/products/drafts",
            json=_snapshot_v2(title="Supplier Source Coat"),
            headers=_headers("source-promotion-draft"),
        )
        assert draft_response.status_code == 201, draft_response.text
        draft = draft_response.json()
        bundle_response = _upload_bundle(
            client,
            [("supplier-front.jpg", _image_bytes(color="navy"), "image/jpeg")],
            draft_revision_id=draft["id"],
        )
        assert bundle_response.status_code == 201, bundle_response.text
        bundle = bundle_response.json()
        asset = bundle["assets"][0]

        revised_payload = _snapshot_v2(title="Supplier Source Coat")
        revised_payload["current_draft_id"] = draft["id"]
        revised_payload["expected_draft_version"] = 1
        revised_draft_response = client.put(
            f"/api/admin/catalog/v2/products/{draft['product_id']}/draft",
            json=revised_payload,
            headers=_headers("revise-before-source-promotion"),
        )
        assert revised_draft_response.status_code == 201, revised_draft_response.text
        revised_draft = revised_draft_response.json()

        promotion_url = (
            f"/api/admin/catalog/source-bundles/{bundle['id']}/assets/{asset['id']}/promote"
        )

        write_public_image = catalog_sources._write_public_image
        write_count = 0

        def fail_second_public_write(content, destination, mime_type):
            nonlocal write_count
            write_count += 1
            if write_count == 2:
                raise OSError("product image storage unavailable")
            return write_public_image(content, destination, mime_type)

        monkeypatch.setattr(
            catalog_sources,
            "_write_public_image",
            fail_second_public_write,
        )
        failed_promotion = client.post(
            promotion_url,
            json={"draft_id": revised_draft["id"], "expected_draft_version": 2},
            headers=_headers("promote-source-storage-failure"),
        )
        assert failed_promotion.status_code == 503
        assert not list(product_dir.glob("*"))
        assert client.get(
            f"/api/admin/catalog/source-bundles/{bundle['id']}"
        ).json()["assets"][0]["status"] == "ready"
        monkeypatch.setattr(
            catalog_sources,
            "_write_public_image",
            write_public_image,
        )

        promoted = client.post(
            promotion_url,
            json={"draft_id": revised_draft["id"], "expected_draft_version": 2},
            headers=_headers("promote-source-asset"),
        )
        assert promoted.status_code == 201, promoted.text
        assert promoted.headers["cache-control"] == "private, no-store"
        result = promoted.json()
        assert result["asset"]["status"] == "promoted"
        assert result["asset"]["promoted_media_id"] == result["media_id"]
        assert result["draft"]["id"] != draft["id"]

        replay = client.post(
            promotion_url,
            json={"draft_id": revised_draft["id"], "expected_draft_version": 2},
            headers=_headers("promote-source-asset"),
        )
        assert replay.status_code == 201
        assert replay.json()["media_id"] == result["media_id"]
        assert replay.json()["draft"]["id"] == result["draft"]["id"]

        with sessions() as db:
            promoted_revision = db.get(CatalogDraftRevision, result["draft"]["id"])
            product = ProductDraftV2.model_validate(promoted_revision.snapshot_json)
            assert [(row.store_id, row.size, row.inventory_qty) for row in product.inventory] == [
                ("1001", None, 8)
            ]
            media = next(item for item in product.media if item.media_id == result["media_id"])
            assert media.role == "variation"
            assert media.approval_status == "approved"
            assert media.provenance == {
                "source": "supplier_asset",
                "source_bundle_id": bundle["id"],
                "source_asset_id": asset["id"],
            }
            assert media.image_set["primary_url"].startswith(
                "https://catalog.example/product-images/"
            )
            assert asset["id"] not in media.media_id
            assert asset["id"] not in media.image_set["primary_url"]
            assert media.image_set["file_path"].startswith(str(product_dir))

        assert len(list(product_dir.glob(f"*{result['media_id']}*"))) == 2
        bundle_after = client.get(
            f"/api/admin/catalog/source-bundles/{bundle['id']}"
        ).json()
        assert bundle_after["draft_revision_id"] == result["draft"]["id"]
        assert "file_path" not in str(bundle_after)

        published = client.post(
            f"/api/admin/catalog/v2/products/{draft['product_id']}/publish",
            json={"draft_id": result["draft"]["id"], "expected_version": 0},
            headers=_headers("publish-source-media"),
        )
        assert published.status_code == 200, published.text
        public_product = client.get(f"/api/products/{draft['product_id']}")
        assert public_product.status_code == 200
        public_payload = public_product.text
        assert bundle["id"] not in public_payload
        assert asset["id"] not in public_payload
        assert asset["original_filename"] not in public_payload


def test_promote_first_supplier_source_becomes_core_media(monkeypatch, tmp_path):
    monkeypatch.setenv("CATALOG_SOURCE_OUTPUT_DIR", str(tmp_path / "private-sources"))
    monkeypatch.setenv("PRODUCT_IMAGE_OUTPUT_DIR", str(tmp_path / "product-images"))
    with _admin_catalog_client(monkeypatch) as (client, sessions):
        draft_payload = _snapshot_v2(title="Supplier-led Product")
        draft_payload["product"]["media"] = []
        draft_response = client.post(
            "/api/admin/catalog/v2/products/drafts",
            json=draft_payload,
            headers=_headers("supplier-led-draft"),
        )
        assert draft_response.status_code == 201
        draft = draft_response.json()
        bundle = _upload_bundle(
            client,
            [("hero.jpg", _image_bytes(), "image/jpeg")],
            draft_revision_id=draft["id"],
        ).json()
        asset = bundle["assets"][0]

        promoted = client.post(
            f"/api/admin/catalog/source-bundles/{bundle['id']}/assets/"
            f"{asset['id']}/promote",
            json={"draft_id": draft["id"], "expected_draft_version": 1},
            headers=_headers("promote-first-source"),
        )
        assert promoted.status_code == 201, promoted.text
        with sessions() as db:
            revision = db.get(CatalogDraftRevision, promoted.json()["draft"]["id"])
            product = ProductDraftV2.model_validate(revision.snapshot_json)
            assert [(media.role, media.approval_status) for media in product.media] == [
                ("core", "approved")
            ]


def test_openapi_exposes_source_routes_only_under_catalog_admin(monkeypatch, tmp_path):
    monkeypatch.setenv("CATALOG_SOURCE_OUTPUT_DIR", str(tmp_path / "private-sources"))
    with _admin_catalog_client(monkeypatch) as (client, _sessions):
        schema = client.get("/openapi.json").json()

    source_paths = {
        path: operations
        for path, operations in schema["paths"].items()
        if "source-bundles" in path
    }
    assert set(source_paths) == {
        "/api/admin/catalog/source-bundles",
        "/api/admin/catalog/source-bundles/{bundle_id}",
        "/api/admin/catalog/source-bundles/{bundle_id}/assets/{asset_id}/preview",
        "/api/admin/catalog/source-bundles/{bundle_id}/assets/{asset_id}",
        "/api/admin/catalog/source-bundles/{bundle_id}/assets/{asset_id}/promote",
    }
    assert all(path.startswith("/api/admin/catalog/") for path in source_paths)
    assert all(
        operation["security"] == [{"ClerkBearer": []}]
        for operations in source_paths.values()
        for operation in operations.values()
        if isinstance(operation, dict) and "responses" in operation
    )


def test_catalog_source_migration_supports_prepublication_drafts_and_downgrade(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'catalog-sources.db'}", future=True)
    with engine.begin() as connection:
        connection.execute(text("pragma foreign_keys=on"))
        connection.execute(
            text("create table catalog_draft_revisions (id varchar(64) primary key)")
        )
        connection.execute(
            text("insert into catalog_draft_revisions (id) values ('draft_new_product')")
        )

    migration_path = (
        Path(__file__).parents[1]
        / "alembic/versions/a2b3c4d5e6f7_add_catalog_source_bundles.py"
    )
    spec = importlib.util.spec_from_file_location("catalog_source_migration", migration_path)
    migration = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(migration)

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with engine.begin() as connection:
        connection.execute(text("pragma foreign_keys=on"))
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        connection.execute(
            text(
                "insert into catalog_source_bundles "
                "(id, owner_provider, owner_provider_user_id, title, catalog_product_id, "
                "draft_revision_id, status) values "
                "('bundle_1', 'clerk', 'admin_1', 'Supplier files', 'cat_future', "
                "'draft_new_product', 'active')"
            )
        )
        connection.execute(
            text(
                "insert into catalog_source_assets "
                "(id, bundle_id, display_order, original_filename, content_type, byte_size, "
                "width, height, checksum_sha256, storage_provider, storage_key, "
                "preview_storage_key, status) values "
                "('asset_1', 'bundle_1', 0, 'front.jpg', 'image/jpeg', 10, 2, 2, "
                "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', "
                "'local_private', 'private/front.jpg', 'private/front-preview.jpg', 'ready')"
            )
        )

    inspector = inspect(engine)
    assert {"catalog_source_bundles", "catalog_source_assets"}.issubset(
        inspector.get_table_names()
    )
    assert "storage_key" in {
        column["name"] for column in inspector.get_columns("catalog_source_assets")
    }
    assert {foreign_key["referred_table"] for foreign_key in inspector.get_foreign_keys(
        "catalog_source_bundles"
    )} == {"catalog_draft_revisions"}

    with engine.begin() as connection:
        connection.execute(text("pragma foreign_keys=on"))
        migration.op = Operations(MigrationContext.configure(connection))
        migration.downgrade()

    assert "catalog_source_assets" not in inspect(engine).get_table_names()
    assert "catalog_source_bundles" not in inspect(engine).get_table_names()
    engine.dispose()
