from pathlib import Path


DEPLOY_WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "deploy-self-hosted.yaml"


def test_production_deployment_enables_authenticated_api_trace_capture():
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")

    assert (
        "API_TRACE_CAPTURE_ENABLED=${{ secrets.API_TRACE_CAPTURE_ENABLED || 'true' }}"
        in workflow
    )
