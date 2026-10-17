"""Service calls through a real router: the read grant gates the health-data endpoints."""

SERVICE = "github.com/imbue-openhost/health-data-service-spec"


def test_service_call_without_read_grant_is_refused(stack):
    consumer = stack.deploy_service_consumer(SERVICE, shortname="health", name="consumer-no-grant")
    result = consumer.call("v1/metrics", method="GET")
    assert result.status == 403
    assert result.body["error"] == "permission_required"
    assert result.body["required_grant"]["grant"] == "read"
    assert "grant_url" in result.body["required_grant"]


def test_service_call_with_read_grant(stack):
    consumer = stack.deploy_service_consumer(
        SERVICE, shortname="health", grants=["read"], grant_manifest_permissions=True, name="consumer-read"
    )
    result = consumer.call("v1/metrics", method="GET")
    assert result.status == 200
    assert "metrics" in result.body
    # The app's own API under /api/ isn't part of the service.
    assert consumer.call("status", method="GET").status == 404
