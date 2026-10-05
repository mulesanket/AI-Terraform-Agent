# tests/test_server.py
import json
import os
import pytest
from unittest.mock import patch


class TestHealth:
    def test_health_returns_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["data"]["status"] == "ok"


class TestAuth:
    def test_read_file_requires_auth(self, client):
        """Without API key header, should return 401."""
        resp = client.post(
            "/read_file",
            json={"path": "/some/path"},
            content_type="application/json",
        )
        assert resp.status_code == 401

    def test_read_file_with_valid_auth(self, client, auth_headers, workdir):
        """With valid API key, request should proceed."""
        import server
        tf_path = os.path.join(workdir, "main.tf")
        with patch.object(server, "AGENT_WORKDIR", workdir):
            resp = client.post("/read_file", json={"path": tf_path}, headers=auth_headers)
        assert resp.status_code == 200

    def test_write_file_requires_auth(self, client):
        resp = client.post(
            "/write_file",
            json={"path": "/some/path", "content": "test"},
            content_type="application/json",
        )
        assert resp.status_code == 401


class TestPathTraversal:
    def test_read_file_blocks_traversal(self, client, auth_headers, workdir):
        import server
        with patch.object(server, "AGENT_WORKDIR", workdir):
            resp = client.post(
                "/read_file",
                json={"path": os.path.join(workdir, "..", "..", "etc", "passwd")},
                headers=auth_headers,
            )
        assert resp.status_code == 403

    def test_write_file_blocks_traversal(self, client, auth_headers, workdir):
        import server
        with patch.object(server, "AGENT_WORKDIR", workdir):
            resp = client.post(
                "/write_file",
                json={
                    "path": os.path.join(workdir, "..", "evil.tf"),
                    "content": "hacked",
                },
                headers=auth_headers,
            )
        assert resp.status_code == 403


class TestFileOps:
    def test_read_file_success(self, client, auth_headers, workdir):
        import server
        tf_path = os.path.join(workdir, "main.tf")
        with patch.object(server, "AGENT_WORKDIR", workdir):
            resp = client.post("/read_file", json={"path": tf_path}, headers=auth_headers)
        data = resp.get_json()
        assert data["ok"] is True
        assert "null_resource" in data["data"]["content"]

    def test_read_file_missing_path(self, client, auth_headers):
        resp = client.post("/read_file", json={}, headers=auth_headers)
        assert resp.status_code == 400

    def test_write_then_read(self, client, auth_headers, workdir):
        import server
        new_file = os.path.join(workdir, "new.tf")
        with patch.object(server, "AGENT_WORKDIR", workdir):
            # Write
            resp = client.post(
                "/write_file",
                json={"path": new_file, "content": "# written by test"},
                headers=auth_headers,
            )
            assert resp.get_json()["ok"] is True
            # Read back
            resp = client.post(
                "/read_file", json={"path": new_file}, headers=auth_headers
            )
            assert "written by test" in resp.get_json()["data"]["content"]


class TestIdentifyDrift:
    def test_identify_drift_with_changes(self, client, auth_headers):
        plan_json = {
            "resource_changes": [
                {
                    "address": "aws_s3_bucket.test",
                    "type": "aws_s3_bucket",
                    "name": "test",
                    "change": {
                        "actions": ["update"],
                        "before": {"tags": {"Env": "dev"}},
                        "after": {"tags": {"Env": "prod"}},
                    },
                }
            ]
        }
        resp = client.post(
            "/identify_drift", json={"plan_json": plan_json}, headers=auth_headers
        )
        data = resp.get_json()
        assert data["ok"] is True
        assert len(data["data"]["changes"]) == 1
        assert data["data"]["changes"][0]["address"] == "aws_s3_bucket.test"

    def test_identify_drift_no_changes(self, client, auth_headers):
        plan_json = {
            "resource_changes": [
                {
                    "address": "aws_s3_bucket.test",
                    "type": "aws_s3_bucket",
                    "name": "test",
                    "change": {"actions": ["no-op"]},
                }
            ]
        }
        resp = client.post(
            "/identify_drift", json={"plan_json": plan_json}, headers=auth_headers
        )
        data = resp.get_json()
        assert data["ok"] is True
        assert len(data["data"]["changes"]) == 0

    def test_identify_drift_missing_input(self, client, auth_headers):
        resp = client.post("/identify_drift", json={}, headers=auth_headers)
        assert resp.status_code == 400
