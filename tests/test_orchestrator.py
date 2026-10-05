# tests/test_orchestrator.py
import os
import json
import pytest
from unittest.mock import patch, MagicMock

# Must set env before importing orchestrator
os.environ.setdefault("AGENT_WORKDIR", ".")
os.environ.setdefault("OPENAI_API_KEY", "test-key")

from orchestrator import HealingOrchestrator


class TestClassifyRisk:
    def setup_method(self):
        self.orch = HealingOrchestrator.__new__(HealingOrchestrator)

    def test_low_risk_update_only(self):
        changes = [
            {"actions": ["update"], "type": "aws_s3_bucket", "address": "aws_s3_bucket.x"},
        ]
        assert self.orch.classify_risk(changes) == "low"

    def test_high_risk_delete(self):
        changes = [
            {"actions": ["delete"], "type": "aws_s3_bucket", "address": "aws_s3_bucket.x"},
        ]
        assert self.orch.classify_risk(changes) == "high"

    def test_high_risk_create(self):
        changes = [
            {"actions": ["create"], "type": "aws_s3_bucket", "address": "aws_s3_bucket.x"},
        ]
        assert self.orch.classify_risk(changes) == "high"

    def test_high_risk_security_type(self):
        changes = [
            {"actions": ["update"], "type": "aws_security_group", "address": "aws_security_group.x"},
        ]
        assert self.orch.classify_risk(changes) == "high"

    def test_empty_changes_low(self):
        assert self.orch.classify_risk([]) == "low"


class TestExtractChanges:
    def test_extracts_update(self):
        plan = {
            "resource_changes": [
                {
                    "address": "aws_s3_bucket.test",
                    "type": "aws_s3_bucket",
                    "name": "test",
                    "change": {
                        "actions": ["update"],
                        "before": {"tags": {}},
                        "after": {"tags": {"Env": "prod"}},
                    },
                }
            ]
        }
        changes = HealingOrchestrator._extract_changes(plan)
        assert len(changes) == 1
        assert changes[0]["address"] == "aws_s3_bucket.test"

    def test_skips_noop(self):
        plan = {
            "resource_changes": [
                {
                    "address": "aws_s3_bucket.test",
                    "type": "aws_s3_bucket",
                    "name": "test",
                    "change": {"actions": ["no-op"]},
                }
            ]
        }
        changes = HealingOrchestrator._extract_changes(plan)
        assert len(changes) == 0

    def test_handles_empty_plan(self):
        assert HealingOrchestrator._extract_changes({}) == []
        assert HealingOrchestrator._extract_changes(None) == []


class TestGenerateFix:
    @patch("orchestrator.get_llm_client")
    def test_generate_fix_calls_openai(self, mock_get_llm):
        mock_client = MagicMock()
        mock_client.chat_hcl.return_value = 'resource "aws_s3_bucket" "test" {}'
        mock_get_llm.return_value = (mock_client, None)

        orch = HealingOrchestrator.__new__(HealingOrchestrator)
        orch.client = mock_client
        orch._llm_err = None

        fix, error = orch.generate_fix(
            [{"address": "aws_s3_bucket.test", "actions": ["update"]}],
            'resource "aws_s3_bucket" "test" { bucket = "old" }',
        )
        assert fix is not None
        assert error is None
        assert "aws_s3_bucket" in fix
        mock_client.chat_hcl.assert_called_once()

    def test_generate_fix_no_api_key(self):
        orch = HealingOrchestrator.__new__(HealingOrchestrator)
        orch.client = None
        orch._llm_err = "LLM not configured"
        fix, error = orch.generate_fix([], "")
        assert fix is None
        assert "not configured" in error


class TestDetectDrift:
    @patch("orchestrator.run_cmd_capture")
    def test_no_drift(self, mock_cmd):
        # rc=0 means no changes
        mock_cmd.return_value = (0, "", "")

        orch = HealingOrchestrator.__new__(HealingOrchestrator)
        orch.workdir = "/tmp/test"
        orch.client = None
        orch._llm_err = "not configured"

        result = orch.detect_drift()
        assert result["has_drift"] is False

    @patch("orchestrator.run_cmd_capture")
    def test_init_failure(self, mock_cmd):
        mock_cmd.return_value = (1, "", "init error")

        orch = HealingOrchestrator.__new__(HealingOrchestrator)
        orch.workdir = "/tmp/test"

        result = orch.detect_drift()
        assert "error" in result
