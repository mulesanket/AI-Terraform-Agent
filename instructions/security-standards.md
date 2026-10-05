# Security Standards

## Objective

All infrastructure must follow security-by-default principles.

---

# Encryption Requirements

## Data at Rest

Must be encrypted using:

- AWS KMS Customer Managed Keys preferred
- AWS Managed Keys acceptable if business approved

Resources requiring encryption:

- S3 Buckets
- EBS Volumes
- RDS Databases
- EFS File Systems
- DynamoDB Tables
- Secrets Manager Secrets
- SQS Queues
- SNS Topics
- EKS Secrets

---

## Data in Transit

Must enforce:

- HTTPS
- TLS 1.2+
- TLS certificates managed through ACM

Never allow:

- Plain HTTP
- Weak ciphers

---

# IAM Standards

## Least Privilege

Grant only required permissions.

Avoid:

- Action: "*"
- Resource: "*"

unless explicitly justified.

---

## Role-Based Access

Prefer:

- IAM Roles
- Federated Access
- OIDC

Avoid:

- IAM Users
- Long-lived access keys

---

## Temporary Credentials

Use:

- AssumeRole
- STS Tokens
- OIDC Federation

---

# Secrets Management

Store secrets only in:

- AWS Secrets Manager
- AWS SSM Parameter Store

Never:

- Hardcode credentials
- Store secrets in Terraform variables
- Commit secrets to Git

---

# Network Security

## Security Groups

Allow only required ports.

Prefer:

- Specific CIDRs
- Security Group References

Avoid:

- 0.0.0.0/0 unless justified

---

## Public Exposure

Public resources require:

- Business justification
- Security review

---

# Logging

Enable:

- CloudTrail
- VPC Flow Logs
- ALB Logs
- S3 Access Logs where applicable

---

# Security Review Checklist

Verify:

- Encryption enabled
- No hardcoded secrets
- Least privilege IAM
- Logging enabled
- Network exposure minimized