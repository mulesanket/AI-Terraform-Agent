# llm.py - LLM provider: Amazon Bedrock
import os
import json
import logging

import requests as http_requests

from config import Config

logger = logging.getLogger("terraform-agent.llm")


def _strip_fences(text):
    """Remove markdown code fences from LLM output."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        end = len(lines) - 1 if lines[-1].strip().startswith("```") else len(lines)
        text = "\n".join(lines[1:end])
    return text.strip()


class LLMClient:
    """
    LLM client for Amazon Bedrock.
    Supports Bearer token auth (short-term API keys) with auto-refresh,
    and falls back to boto3 credential chain (SSO profile / instance role).
    """

    def __init__(self):
        self.provider = Config.LLM_PROVIDER.lower()
        self._client = None  # boto3 client (fallback only)
        self._use_bearer = False
        self._token_provider = None  # auto-refresh function

        if self.provider == "bedrock":
            self._init_bedrock()
        else:
            raise ValueError(
                f"Unknown LLM_PROVIDER: {self.provider!r}. Use 'bedrock'."
            )

    def _init_bedrock(self):
        self.model_id = os.environ.get("BEDROCK_MODEL_ID") or Config.BEDROCK_MODEL_ID
        self._region = os.environ.get("BEDROCK_REGION") or Config.BEDROCK_REGION
        self._endpoint = (
            f"https://bedrock-runtime.{self._region}.amazonaws.com"
        )

        # Priority 1: Bearer token (short-term API key)
        bearer_token = os.environ.get("AWS_BEARER_TOKEN_BEDROCK") or Config.AWS_BEARER_TOKEN_BEDROCK
        if bearer_token:
            self._use_bearer = True
            # Ensure region is available in env for any downstream libs
            os.environ["AWS_REGION"] = self._region
            # Use the token directly — it's already a valid bearer token
            self._static_token = bearer_token
            self._token_provider = None
            auth_method = "bearer-token"

            logger.info(
                "LLM provider: Amazon Bedrock (model=%s, region=%s, auth=%s)",
                self.model_id, self._region, auth_method,
            )
            return

        # Priority 2: boto3 credential chain (SSO profile, instance role, etc.)
        try:
            import boto3
        except ImportError:
            raise ImportError("boto3 is required for Bedrock. Run: pip install boto3")

        if Config.AWS_PROFILE:
            session = boto3.Session(
                profile_name=Config.AWS_PROFILE,
                region_name=self._region,
            )
            auth_method = f"profile:{Config.AWS_PROFILE}"
        else:
            session = boto3.Session(region_name=self._region)
            auth_method = "default-chain"

        self._client = session.client("bedrock-runtime")
        logger.info(
            "LLM provider: Amazon Bedrock (model=%s, region=%s, auth=%s)",
            self.model_id, self._region, auth_method,
        )

    def _get_bearer_token(self):
        """Return a valid Bearer token, using auto-refresh if available."""
        if self._token_provider:
            return self._token_provider()
        return self._static_token

    def _extract_text(self, data):
        """Extract text from Bedrock Converse response, handling different content block formats."""
        try:
            content = data["output"]["message"]["content"]
            for block in content:
                if isinstance(block, str):
                    return block
                if isinstance(block, dict):
                    if "text" in block:
                        return block["text"]
            # Fallback: return the first block as string
            return str(content[0])
        except (KeyError, IndexError, TypeError) as exc:
            logger.error("Failed to parse LLM response: %s | raw: %s", exc, json.dumps(data, default=str)[:1000])
            raise RuntimeError(f"Unexpected LLM response format: {exc}")

    def chat(self, system, user, temperature=0.1, max_tokens=8192):
        """Send a chat request. Returns the raw response text."""
        return self.chat_with_metadata(system, user, temperature, max_tokens)["text"]

    def chat_with_metadata(self, system, user, temperature=0.1, max_tokens=8192):
        """Send a chat request and return the text plus token usage metadata."""
        if self._use_bearer:
            return self._chat_bearer(system, user, temperature, max_tokens)
        return self._chat_boto3(system, user, temperature, max_tokens)

    def chat_hcl(self, system, user):
        """Sends a chat request and strips markdown fences from the response."""
        return _strip_fences(self.chat(system, user))

    def chat_hcl_with_metadata(self, system, user, temperature=0.1, max_tokens=8192):
        """Return stripped HCL text plus any Bedrock token usage metadata."""
        result = self.chat_with_metadata(system, user, temperature, max_tokens)
        result["text"] = _strip_fences(result["text"])
        return result

    def _chat_bearer(self, system, user, temperature, max_tokens):
        """Call Bedrock Converse API via REST with Bearer token auth."""
        url = f"{self._endpoint}/model/{self.model_id}/converse"
        token = self._get_bearer_token()

        payload = {
            "system": [{"text": system}],
            "messages": [{"role": "user", "content": [{"text": user}]}],
            "inferenceConfig": {
                "maxTokens": max_tokens,
            },
        }

        resp = http_requests.post(
            url,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            json=payload,
            timeout=120,
        )

        if resp.status_code != 200:
            raise RuntimeError(
                f"Bedrock API error {resp.status_code}: {resp.text}"
            )

        data = resp.json()
        logger.debug("Bedrock raw response: %s", json.dumps(data, default=str)[:2000])
        return self._extract_text_and_metadata(data)

    def _chat_boto3(self, system, user, temperature, max_tokens):
        """Call Bedrock Converse API via boto3 SDK."""
        response = self._client.converse(
            modelId=self.model_id,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": user}]}],
            inferenceConfig={"maxTokens": max_tokens},
        )
        logger.debug("Bedrock raw response: %s", json.dumps(response, default=str)[:2000])
        return self._extract_text_and_metadata(response)

    def _extract_text_and_metadata(self, data):
        text = self._extract_text(data)
        usage = data.get("usage", {}) if isinstance(data, dict) else {}
        input_tokens = usage.get("inputTokens") or usage.get("input_tokens")
        output_tokens = usage.get("outputTokens") or usage.get("output_tokens")
        total_tokens = usage.get("totalTokens") or usage.get("total_tokens")
        return {
            "text": text,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
            },
            "raw_usage": usage,
            "model_id": self.model_id,
        }


def get_llm_client():
    """Return (LLMClient, None) on success or (None, error_str) on failure."""
    try:
        return LLMClient(), None
    except Exception as e:
        logger.error("Failed to init LLM client: %s", e)
        return None, str(e)
