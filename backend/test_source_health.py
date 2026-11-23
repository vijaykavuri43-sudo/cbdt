import httpx
import pytest

from . import app as app_module, source_health


@pytest.mark.parametrize("code,state,forbidden", [(200, "accessible", False), (403, "forbidden", True), (404, "unavailable", False), (503, "unavailable", False)])
def test_upstream_http_status_is_not_confused_with_mirror(monkeypatch, code, state, forbidden):
    real_client = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(code, request=request))
    monkeypatch.setattr(source_health.httpx, "Client", lambda **kwargs: real_client(transport=transport, **kwargs))
    result = source_health.probe_source()
    assert result["state"] == state and result["http_status"] == code
    assert result["forbidden"] is forbidden
    assert result["accessible"] == (code == 200)
    if forbidden:
        assert "Preserved mirror evidence remains available" in result["message"]


def test_timeout_is_unknown_not_forbidden(monkeypatch):
    def fail(request):
        raise httpx.ReadTimeout("timeout")
    real_client = httpx.Client
    transport = httpx.MockTransport(fail)
    monkeypatch.setattr(source_health.httpx, "Client", lambda **kwargs: real_client(transport=transport, **kwargs))
    result = source_health.probe_source()
    assert result["state"] == "unknown" and not result["forbidden"]


def test_forbidden_upstream_overrides_misleading_sync_verdict(monkeypatch):
    upstream = dict(forbidden=True, message="UPSTREAM S3 FORBIDDEN (403)")
    monkeypatch.setattr(app_module, "source_health", lambda: upstream)
    monkeypatch.setattr(app_module, "object_store", object())
    result = app_module.mirror_sync()
    assert result["healthy"] is False
    assert result["verdict"] == upstream["message"]


def test_secondary_dashboards_explicitly_select_their_own_upstream():
    from pathlib import Path
    compose = (Path(__file__).parents[1] / "docker-compose.yml").read_text()
    for service, bucket in (("s3lm-b2", "bxs3dev001"), ("s3lm-b3", "tw-artifacts-center"), ("s3lm-b4", "bx-s3-dev-002")):
        section = compose.split(f"\n  {service}:\n", 1)[1].split("\n  s3sync", 1)[0]
        assert f"S3LM_BUCKET_URL=https://{bucket}.s3.ap-southeast-1.amazonaws.com" in section
