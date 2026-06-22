from pathlib import Path


DEPLOY_WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "deploy-self-hosted.yaml"
DEPLOY_COMPOSE = Path(__file__).parents[1] / "deploy" / "docker-compose.prod.yml"
DOCKERFILE = Path(__file__).parents[1] / "Dockerfile"
DAILY_SYNTHETIC_SCRIPT = Path(__file__).parents[1] / "scripts" / "run_daily_synthetic_orders.sh"


def test_production_deployment_enables_authenticated_api_trace_capture():
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")

    assert (
        "API_TRACE_CAPTURE_ENABLED=${{ secrets.API_TRACE_CAPTURE_ENABLED || 'true' }}"
        in workflow
    )


def test_production_compose_includes_daily_synthetic_order_scheduler():
    compose = DEPLOY_COMPOSE.read_text(encoding="utf-8")
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "daily-synthetic-orders:" in compose
    assert "command: ./scripts/run_daily_synthetic_orders.sh" in compose
    assert "restart: unless-stopped" in compose
    assert "products_data:/app/data" in compose
    assert "RUN chmod +x /app/scripts/run_daily_synthetic_orders.sh" in dockerfile


def test_production_deployment_enables_daily_synthetic_order_scheduler():
    compose = DEPLOY_COMPOSE.read_text(encoding="utf-8")
    script = DAILY_SYNTHETIC_SCRIPT.read_text(encoding="utf-8")

    assert "SYNTHETIC_DAILY_ORDERS_ENABLED: ${SYNTHETIC_DAILY_ORDERS_ENABLED:-true}" in compose
    assert "SYNTHETIC_DAILY_RUN_ON_STARTUP: ${SYNTHETIC_DAILY_RUN_ON_STARTUP:-true}" in compose
    assert 'case "${SYNTHETIC_DAILY_RUN_ON_STARTUP:-true}" in' in script
    assert "run_daily_order_refresh" in script
