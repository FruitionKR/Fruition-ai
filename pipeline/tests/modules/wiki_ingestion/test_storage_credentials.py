"""외부 연결 없이 실제 SDK 서명·STS web identity·로컬 자격증명을 검증한다."""

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest
from minio.credentials.providers import IamAwsProvider

from app.modules.wiki_ingestion.infrastructure import object_storage as storage


@pytest.fixture
def endpoint():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def do_GET(self):
            requests.append((self.path, dict(self.headers)))
            data = (b'<LocationConstraint xmlns="http://s3.amazonaws.com/doc/2006-03-01/"></LocationConstraint>'
                    if "location" in self.path else b"saved log")
            self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            requests.append((self.path + body.decode(), dict(self.headers)))
            data = b'''<AssumeRoleWithWebIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/"><AssumeRoleWithWebIdentityResult><Credentials><AccessKeyId>web-identity-access</AccessKeyId><SecretAccessKey>web-identity-secret</SecretAccessKey><SessionToken>web-identity-session</SessionToken><Expiration>2099-01-01T00:00:00Z</Expiration></Credentials></AssumeRoleWithWebIdentityResult></AssumeRoleWithWebIdentityResponse>'''
            self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try: yield f"http://127.0.0.1:{server.server_port}", requests
    finally: server.shutdown(); server.server_close(); thread.join()


def test_local_and_aws_temporary_credentials_sign_requests(endpoint):
    url, requests = endpoint
    with patch.dict(os.environ, {"S3_ENDPOINT":url,"S3_BUCKET":"test-bucket",
                                 "S3_ACCESS_KEY":"local-access","S3_SECRET_KEY":"local-secret"}, clear=True):
        assert storage.read_text_object("wiki/a") == "saved log"
        assert "local-access" in requests[-1][1]["Authorization"]
        assert "X-Amz-Security-Token" not in requests[-1][1]
        os.environ.update(S3_CREDENTIALS_MODE="aws",AWS_REGION="ap-northeast-2",
                          AWS_ACCESS_KEY_ID="temporary-access",AWS_SECRET_ACCESS_KEY="temporary-secret",
                          AWS_SESSION_TOKEN="temporary-session")
        assert storage.read_text_object("wiki/a") == "saved log"
        assert "temporary-access" in requests[-1][1]["Authorization"]
        assert "ap-northeast-2" in requests[-1][1]["Authorization"]
        assert requests[-1][1]["X-Amz-Security-Token"] == "temporary-session"


def test_web_identity_uses_sdk_sts_provider(endpoint, tmp_path):
    url, requests = endpoint
    token = tmp_path / "token"; token.write_text("test.jwt.token")
    with patch.dict(os.environ, {"S3_ENDPOINT":url,"S3_BUCKET":"test-bucket","S3_CREDENTIALS_MODE":"aws",
                                 "AWS_REGION":"ap-northeast-2", "AWS_WEB_IDENTITY_TOKEN_FILE":str(token),
                                 "AWS_ROLE_ARN":"arn:aws:iam::123456789012:role/pipeline"}, clear=True), \
            patch.object(storage, "IamAwsProvider", lambda: IamAwsProvider(custom_endpoint=url)):
        assert storage.read_text_object("pipeline-runs/run/pipeline.log") == "saved log"
        assert any("AssumeRoleWithWebIdentity" in body for body, _ in requests)
        assert "web-identity-access" in requests[-1][1]["Authorization"]
        assert requests[-1][1]["X-Amz-Security-Token"] == "web-identity-session"


def test_aws_never_uses_local_key_or_creates_bucket():
    with patch.dict(os.environ, {"S3_CREDENTIALS_MODE":"aws","AWS_REGION":"ap-northeast-2",
                                 "S3_ACCESS_KEY":"local-key","S3_SECRET_KEY":"local-secret"}, clear=True), \
            patch.object(IamAwsProvider, "retrieve", side_effect=ValueError("no identity")):
        with pytest.raises(ValueError): storage.client()._provider.retrieve()
        with patch.object(storage, "client") as factory:
            storage.write_text_object("wiki/a", "content")
            factory.return_value.bucket_exists.assert_not_called()
            factory.return_value.make_bucket.assert_not_called()
            factory.return_value.put_object.assert_called_once()
