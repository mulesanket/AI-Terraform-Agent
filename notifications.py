# notifications.py - Alert dispatching for the Self-Healing Terraform Agent
import json
import logging
import requests

logger = logging.getLogger("terraform-agent.notifications")


def send_slack(webhook_url, message):
    """Send a message to a Slack incoming webhook."""
    if not webhook_url:
        logger.debug("No Slack webhook configured, skipping notification")
        return
    try:
        resp = requests.post(
            webhook_url,
            json={"text": message},
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning("Slack webhook returned %d: %s", resp.status_code, resp.text)
    except Exception as e:
        logger.error("Failed to send Slack notification: %s", e)


def notify(event_type, details, slack_webhook_url=None):
    """
    Dispatch a notification to all configured channels.

    event_type: drift_detected | fix_generated | fix_applied | fix_failed |
                pr_created | approval_needed | cost_exceeded | policy_violation
    details: dict with context about the event
    """
    icon_map = {
        "drift_detected": "[DRIFT]",
        "fix_generated": "[FIX]",
        "fix_applied": "[APPLIED]",
        "fix_failed": "[FAILED]",
        "pr_created": "[PR]",
        "approval_needed": "[REVIEW]",
        "cost_exceeded": "[COST]",
        "policy_violation": "[POLICY]",
    }
    icon = icon_map.get(event_type, "[INFO]")
    summary = details.get("summary", json.dumps(details, default=str))
    message = f"{icon} *{event_type.replace('_', ' ').title()}*\n{summary}"

    logger.info("Notification [%s]: %s", event_type, summary)

    # Slack
    send_slack(slack_webhook_url, message)
