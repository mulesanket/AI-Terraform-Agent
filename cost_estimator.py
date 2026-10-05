# cost_estimator.py — Free AWS cost estimation using the AWS Pricing API
# Replaces Infracost with zero licensing cost.
# The AWS Pricing API (pricing.us-east-1.amazonaws.com) is free to call.

import json
import logging
import re
from typing import Optional

import boto3

from config import Config

logger = logging.getLogger("terraform-agent.cost")

# AWS Pricing API is only available in us-east-1 and ap-south-1
PRICING_REGION = "us-east-1"

# ─── Pricing cache (in-memory, per-session) ───────────────────────────────────
_price_cache: dict = {}

# Fallback on-demand hourly prices (us-east-1, Linux) when Pricing API is unavailable
_EC2_FALLBACK_PRICES = {
    "t3.nano": 0.0052, "t3.micro": 0.0104, "t3.small": 0.0208, "t3.medium": 0.0416,
    "t3.large": 0.0832, "t3.xlarge": 0.1664, "t3.2xlarge": 0.3328,
    "t3a.nano": 0.0047, "t3a.micro": 0.0094, "t3a.small": 0.0188, "t3a.medium": 0.0376,
    "t3a.large": 0.0752, "t3a.xlarge": 0.1504, "t3a.2xlarge": 0.3008,
    "t2.nano": 0.0058, "t2.micro": 0.0116, "t2.small": 0.023, "t2.medium": 0.0464,
    "t2.large": 0.0928, "t2.xlarge": 0.1856, "t2.2xlarge": 0.3712,
    "m5.large": 0.096, "m5.xlarge": 0.192, "m5.2xlarge": 0.384, "m5.4xlarge": 0.768,
    "m6i.large": 0.096, "m6i.xlarge": 0.192, "m6i.2xlarge": 0.384,
    "m7i.large": 0.1008, "m7i.xlarge": 0.2016, "m7i.2xlarge": 0.4032,
    "c5.large": 0.085, "c5.xlarge": 0.17, "c5.2xlarge": 0.34,
    "c6i.large": 0.085, "c6i.xlarge": 0.17, "c6i.2xlarge": 0.34,
    "r5.large": 0.126, "r5.xlarge": 0.252, "r5.2xlarge": 0.504,
    "r6i.large": 0.126, "r6i.xlarge": 0.252, "r6i.2xlarge": 0.504,
}

_RDS_FALLBACK_PRICES = {
    "db.t3.micro": 0.017, "db.t3.small": 0.034, "db.t3.medium": 0.068,
    "db.t3.large": 0.136, "db.t3.xlarge": 0.272, "db.t3.2xlarge": 0.544,
    "db.t4g.micro": 0.016, "db.t4g.small": 0.032, "db.t4g.medium": 0.065,
    "db.m5.large": 0.171, "db.m5.xlarge": 0.342, "db.m5.2xlarge": 0.684,
    "db.m6i.large": 0.171, "db.m6i.xlarge": 0.342, "db.m6i.2xlarge": 0.684,
    "db.r5.large": 0.24, "db.r5.xlarge": 0.48, "db.r5.2xlarge": 0.96,
}


def _get_pricing_client():
    """Create a Pricing API client using the same auth as the rest of the app."""
    session_kwargs = {"region_name": PRICING_REGION}
    if Config.AWS_PROFILE:
        session_kwargs["profile_name"] = Config.AWS_PROFILE
    session = boto3.Session(**session_kwargs)
    return session.client("pricing", region_name=PRICING_REGION)


def _get_price(client, service_code: str, filters: list) -> Optional[float]:
    """Query AWS Pricing API and extract the on-demand hourly/monthly price."""
    cache_key = f"{service_code}:{json.dumps(filters, sort_keys=True)}"
    if cache_key in _price_cache:
        return _price_cache[cache_key]

    try:
        response = client.get_products(
            ServiceCode=service_code,
            Filters=filters,
            MaxResults=1,
        )
        price_list = response.get("PriceList", [])
        if not price_list:
            return None

        product = json.loads(price_list[0])
        terms = product.get("terms", {}).get("OnDemand", {})
        for term in terms.values():
            for dimension in term.get("priceDimensions", {}).values():
                price_str = dimension.get("pricePerUnit", {}).get("USD", "0")
                price = float(price_str)
                if price > 0:
                    _price_cache[cache_key] = price
                    return price
        return None
    except Exception as e:
        logger.debug("Pricing API error for %s: %s", service_code, e)
        return None


# ─── Resource-specific estimators ─────────────────────────────────────────────

def _estimate_ec2_instance(client, attrs: dict, region: str) -> dict:
    """Estimate EC2 instance cost."""
    instance_type = attrs.get("instance_type", "t3.micro")
    filters = [
        {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
        {"Type": "TERM_MATCH", "Field": "location", "Value": _region_to_location(region)},
        {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": "Linux"},
        {"Type": "TERM_MATCH", "Field": "tenancy", "Value": "Shared"},
        {"Type": "TERM_MATCH", "Field": "preInstalledSw", "Value": "NA"},
        {"Type": "TERM_MATCH", "Field": "capacitystatus", "Value": "Used"},
    ]
    hourly = _get_price(client, "AmazonEC2", filters)
    if hourly:
        monthly = hourly * 730  # avg hours/month
        return {"resource_type": "aws_instance", "instance_type": instance_type,
                "hourly": hourly, "monthly": monthly}
    # Fallback: use published us-east-1 on-demand prices
    fallback_hourly = _EC2_FALLBACK_PRICES.get(instance_type)
    if fallback_hourly:
        return {"resource_type": "aws_instance", "instance_type": instance_type,
                "hourly": fallback_hourly, "monthly": fallback_hourly * 730, "note": "fallback pricing"}
    return {"resource_type": "aws_instance", "instance_type": instance_type,
            "hourly": 0, "monthly": 0, "note": "Price not found"}


def _estimate_ebs_volume(client, attrs: dict, region: str) -> dict:
    """Estimate EBS volume cost."""
    volume_type = attrs.get("type", attrs.get("volume_type", "gp3"))
    size_gb = int(attrs.get("size", attrs.get("volume_size", 8)))

    # Mapping volume types to pricing API values
    vol_api_map = {
        "gp3": "General Purpose", "gp2": "General Purpose",
        "io1": "Provisioned IOPS", "io2": "Provisioned IOPS",
        "st1": "Throughput Optimized HDD", "sc1": "Cold HDD",
        "standard": "Magnetic",
    }
    vol_desc = vol_api_map.get(volume_type, "General Purpose")

    filters = [
        {"Type": "TERM_MATCH", "Field": "location", "Value": _region_to_location(region)},
        {"Type": "TERM_MATCH", "Field": "volumeApiName", "Value": volume_type},
    ]
    price_per_gb = _get_price(client, "AmazonEC2", filters)
    if price_per_gb:
        monthly = price_per_gb * size_gb
        return {"resource_type": "aws_ebs_volume", "volume_type": volume_type,
                "size_gb": size_gb, "monthly": monthly}
    # Fallback estimates (us-east-1 published prices)
    fallback = {"gp3": 0.08, "gp2": 0.10, "io1": 0.125, "io2": 0.125, "st1": 0.045, "sc1": 0.015}
    price = fallback.get(volume_type, 0.08)
    return {"resource_type": "aws_ebs_volume", "volume_type": volume_type,
            "size_gb": size_gb, "monthly": price * size_gb}


def _estimate_rds_instance(client, attrs: dict, region: str) -> dict:
    """Estimate RDS instance cost."""
    instance_class = attrs.get("instance_class", "db.t3.micro")
    engine = attrs.get("engine", "mysql")

    engine_map = {
        "mysql": "MySQL", "postgres": "PostgreSQL", "mariadb": "MariaDB",
        "aurora-mysql": "Aurora MySQL", "aurora-postgresql": "Aurora PostgreSQL",
        "oracle-ee": "Oracle", "sqlserver-ex": "SQL Server",
    }
    db_engine = engine_map.get(engine, engine)

    filters = [
        {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_class},
        {"Type": "TERM_MATCH", "Field": "location", "Value": _region_to_location(region)},
        {"Type": "TERM_MATCH", "Field": "databaseEngine", "Value": db_engine},
        {"Type": "TERM_MATCH", "Field": "deploymentOption", "Value": "Single-AZ"},
    ]
    hourly = _get_price(client, "AmazonRDS", filters)
    if hourly:
        monthly = hourly * 730
        return {"resource_type": "aws_db_instance", "instance_class": instance_class,
                "engine": engine, "hourly": hourly, "monthly": monthly}
    # Fallback pricing
    fallback_hourly = _RDS_FALLBACK_PRICES.get(instance_class)
    if fallback_hourly:
        return {"resource_type": "aws_db_instance", "instance_class": instance_class,
                "engine": engine, "hourly": fallback_hourly, "monthly": fallback_hourly * 730, "note": "fallback pricing"}
    return {"resource_type": "aws_db_instance", "instance_class": instance_class,
            "engine": engine, "hourly": 0, "monthly": 0, "note": "Price not found"}


def _estimate_nat_gateway(client, attrs: dict, region: str) -> dict:
    """Estimate NAT Gateway cost (fixed hourly rate)."""
    filters = [
        {"Type": "TERM_MATCH", "Field": "location", "Value": _region_to_location(region)},
        {"Type": "TERM_MATCH", "Field": "usagetype", "Value": f"{_region_prefix(region)}-NatGateway-Hours"},
    ]
    hourly = _get_price(client, "AmazonEC2", filters)
    monthly = (hourly or 0.045) * 730  # $0.045/hr is us-east-1 default
    return {"resource_type": "aws_nat_gateway", "hourly": hourly or 0.045, "monthly": monthly}


def _estimate_lb(client, attrs: dict, region: str) -> dict:
    """Estimate Load Balancer cost."""
    lb_type = attrs.get("load_balancer_type", "application")
    # ALB ~$0.0225/hr, NLB ~$0.0225/hr in us-east-1
    hourly_fallback = 0.0225
    filters = [
        {"Type": "TERM_MATCH", "Field": "location", "Value": _region_to_location(region)},
        {"Type": "TERM_MATCH", "Field": "usagetype", "Value": f"{_region_prefix(region)}-LoadBalancerUsage"},
    ]
    hourly = _get_price(client, "ElasticLoadBalancing", filters) or hourly_fallback
    monthly = hourly * 730
    return {"resource_type": "aws_lb", "lb_type": lb_type, "hourly": hourly, "monthly": monthly}


def _estimate_elasticache(client, attrs: dict, region: str) -> dict:
    """Estimate ElastiCache node cost."""
    node_type = attrs.get("node_type", "cache.t3.micro")
    engine = attrs.get("engine", "redis")
    filters = [
        {"Type": "TERM_MATCH", "Field": "instanceType", "Value": node_type},
        {"Type": "TERM_MATCH", "Field": "location", "Value": _region_to_location(region)},
        {"Type": "TERM_MATCH", "Field": "cacheEngine", "Value": engine.capitalize()},
    ]
    hourly = _get_price(client, "AmazonElastiCache", filters)
    if hourly:
        num_nodes = int(attrs.get("num_cache_nodes", 1))
        monthly = hourly * 730 * num_nodes
        return {"resource_type": "aws_elasticache_cluster", "node_type": node_type,
                "nodes": num_nodes, "hourly": hourly, "monthly": monthly}
    return {"resource_type": "aws_elasticache_cluster", "node_type": node_type,
            "hourly": 0, "monthly": 0, "note": "Price not found"}


# ─── Fixed-price / usage-based resources ──────────────────────────────────────

FIXED_MONTHLY_ESTIMATES = {
    "aws_eip": 3.60,          # $0.005/hr when not attached
    "aws_kms_key": 1.00,      # $1/month per CMK
    "aws_secretsmanager_secret": 0.40,  # $0.40/month per secret
    "aws_cloudwatch_log_group": 0.0,    # pay per ingestion (usage-based)
    "aws_sns_topic": 0.0,     # free tier generous
    "aws_sqs_queue": 0.0,     # free tier generous
    "aws_s3_bucket": 0.0,     # usage-based (storage + requests)
    "aws_dynamodb_table": 0.0,  # on-demand = usage-based
    "aws_lambda_function": 0.0,  # usage-based
}


# ─── Main estimation function ─────────────────────────────────────────────────

def estimate_from_plan(plan_json: dict, region: str = None) -> dict:
    """
    Parse a terraform plan/show JSON and estimate monthly costs.
    Returns a structured cost breakdown.
    """
    if not region:
        region = Config.BEDROCK_REGION or "us-east-1"

    try:
        client = _get_pricing_client()
    except Exception as e:
        logger.error("Cannot create Pricing API client: %s", e)
        return {"error": str(e), "total_monthly": 0, "resources": []}

    resources = []
    total_monthly = 0.0

    # Extract resource changes from plan
    resource_changes = plan_json.get("resource_changes", [])
    if not resource_changes:
        # Try planned_values for state-based estimation
        root_module = plan_json.get("planned_values", {}).get("root_module", {})
        resource_changes = root_module.get("resources", [])

    for rc in resource_changes:
        actions = rc.get("change", {}).get("actions", ["create"]) if "change" in rc else ["create"]
        if actions == ["delete"]:
            continue  # Don't count resources being destroyed

        res_type = rc.get("type", "")
        address = rc.get("address", rc.get("name", "unknown"))
        attrs = rc.get("change", {}).get("after", {}) if "change" in rc else rc.get("values", {})
        if not attrs:
            attrs = {}

        estimate = None

        if res_type == "aws_instance":
            estimate = _estimate_ec2_instance(client, attrs, region)
            # Add root volume cost
            root_bd = attrs.get("root_block_device", [])
            if root_bd and isinstance(root_bd, list) and root_bd:
                vol_attrs = root_bd[0] if isinstance(root_bd[0], dict) else {}
                vol_est = _estimate_ebs_volume(client, vol_attrs, region)
                estimate["monthly"] += vol_est.get("monthly", 0)
                estimate["includes_ebs"] = True
                estimate["ebs_monthly"] = vol_est.get("monthly", 0)

        elif res_type == "aws_ebs_volume":
            estimate = _estimate_ebs_volume(client, attrs, region)

        elif res_type == "aws_db_instance":
            estimate = _estimate_rds_instance(client, attrs, region)

        elif res_type == "aws_nat_gateway":
            estimate = _estimate_nat_gateway(client, attrs, region)

        elif res_type in ("aws_lb", "aws_alb"):
            estimate = _estimate_lb(client, attrs, region)

        elif res_type == "aws_elasticache_cluster":
            estimate = _estimate_elasticache(client, attrs, region)

        elif res_type in FIXED_MONTHLY_ESTIMATES:
            monthly = FIXED_MONTHLY_ESTIMATES[res_type]
            estimate = {"resource_type": res_type, "monthly": monthly}
            if monthly == 0:
                estimate["note"] = "Usage-based pricing (not estimable without usage data)"

        if estimate:
            estimate["address"] = address
            monthly_cost = estimate.get("monthly", 0)
            total_monthly += monthly_cost
            resources.append(estimate)

    return {
        "total_monthly": round(total_monthly, 2),
        "total_hourly": round(total_monthly / 730, 4),
        "currency": "USD",
        "region": region,
        "resource_count": len(resources),
        "resources": resources,
    }


def estimate_from_workdir(workdir: str) -> dict:
    """Run terraform show -json on existing plan and estimate costs."""
    import subprocess
    import os

    plan_path = os.path.join(workdir, ".generated", "plan.out")
    if not os.path.exists(plan_path):
        plan_path = os.path.join(workdir, "plan.out")
    if not os.path.exists(plan_path):
        return {"error": "No plan.out found. Run terraform plan first.", "total_monthly": 0, "resources": []}

    try:
        result = subprocess.run(
            ["terraform", "show", "-json", plan_path],
            cwd=workdir if not workdir.endswith(".generated") else os.path.dirname(plan_path),
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            return {"error": f"terraform show failed: {result.stderr[:200]}", "total_monthly": 0, "resources": []}
        plan_json = json.loads(result.stdout)
        return estimate_from_plan(plan_json)
    except Exception as e:
        return {"error": str(e), "total_monthly": 0, "resources": []}


def format_cost_markdown(estimate: dict) -> str:
    """Format cost estimate as a Markdown PR comment."""
    if estimate.get("error"):
        return f"## \U0001f4b0 Cost Estimate\n\n\u26a0\ufe0f Could not estimate costs: {estimate['error']}"

    total = estimate["total_monthly"]
    hourly = estimate["total_hourly"]
    resources = estimate.get("resources", [])

    lines = [
        "## \U0001f4b0 Cost Estimate",
        "",
        f"**Monthly Estimate: ${total:,.2f}**",
        f"**Hourly Estimate: ${hourly:,.4f}**",
        f"**Region:** {estimate.get('region', 'us-east-1')}",
        "",
    ]

    # Cost table for resources with price > 0
    priced = [r for r in resources if r.get("monthly", 0) > 0]
    if priced:
        lines.append("| Resource | Type | Details | Monthly Cost |")
        lines.append("|---|---|---|---:|")
        for r in sorted(priced, key=lambda x: x.get("monthly", 0), reverse=True):
            addr = r.get("address", "")
            rtype = r.get("resource_type", "")
            details = _resource_details(r)
            monthly = r.get("monthly", 0)
            lines.append(f"| `{addr}` | {rtype} | {details} | ${monthly:,.2f} |")
        lines.append("")

    # Usage-based resources
    usage_based = [r for r in resources if r.get("note") and "usage-based" in r.get("note", "").lower()]
    if usage_based:
        lines.append("**Usage-based resources** (cost depends on traffic/storage):")
        for r in usage_based:
            lines.append(f"- `{r.get('address', '')}` ({r.get('resource_type', '')})")
        lines.append("")

    if not priced and not usage_based:
        lines.append("_No cost-bearing resources detected (free tier or usage-based pricing)._")
        lines.append("")

    lines.extend([
        "---",
        "<sub>\U0001f4a1 Estimated using AWS Pricing API (on-demand, no reserved/spot discounts). "
        "Actual costs may vary based on usage, data transfer, and request volume.</sub>",
    ])

    return "\n".join(lines)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _resource_details(r: dict) -> str:
    """Generate a short details string for a resource."""
    parts = []
    if r.get("instance_type"):
        parts.append(r["instance_type"])
    if r.get("instance_class"):
        parts.append(r["instance_class"])
    if r.get("engine"):
        parts.append(r["engine"])
    if r.get("volume_type"):
        parts.append(f"{r['volume_type']}/{r.get('size_gb', '?')}GB")
    if r.get("lb_type"):
        parts.append(r["lb_type"])
    if r.get("node_type"):
        parts.append(f"{r['node_type']} x{r.get('nodes', 1)}")
    if r.get("includes_ebs"):
        parts.append(f"+EBS ${r.get('ebs_monthly', 0):.2f}")
    return ", ".join(parts) if parts else "—"


# Region name mapping for Pricing API
_REGION_NAMES = {
    "us-east-1": "US East (N. Virginia)",
    "us-east-2": "US East (Ohio)",
    "us-west-1": "US West (N. California)",
    "us-west-2": "US West (Oregon)",
    "eu-west-1": "EU (Ireland)",
    "eu-west-2": "EU (London)",
    "eu-west-3": "EU (Paris)",
    "eu-central-1": "EU (Frankfurt)",
    "eu-north-1": "EU (Stockholm)",
    "ap-south-1": "Asia Pacific (Mumbai)",
    "ap-southeast-1": "Asia Pacific (Singapore)",
    "ap-southeast-2": "Asia Pacific (Sydney)",
    "ap-northeast-1": "Asia Pacific (Tokyo)",
    "ap-northeast-2": "Asia Pacific (Seoul)",
    "ca-central-1": "Canada (Central)",
    "sa-east-1": "South America (Sao Paulo)",
}

_REGION_PREFIXES = {
    "us-east-1": "USE1",
    "us-east-2": "USE2",
    "us-west-1": "USW1",
    "us-west-2": "USW2",
    "eu-west-1": "EUW1",
    "eu-central-1": "EUC1",
    "ap-south-1": "APS1",
    "ap-southeast-1": "APS1",
    "ap-northeast-1": "APN1",
}


def _region_to_location(region: str) -> str:
    return _REGION_NAMES.get(region, "US East (N. Virginia)")


def _region_prefix(region: str) -> str:
    return _REGION_PREFIXES.get(region, "USE1")
