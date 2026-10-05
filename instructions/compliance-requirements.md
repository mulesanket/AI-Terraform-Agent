# Compliance Requirements

## Objective

Infrastructure must comply with organizational and regulatory requirements.

---

# Mandatory Controls

- Encryption at Rest
- Encryption in Transit
- Audit Logging
- IAM Least Privilege
- Backup Strategy

---

# Logging

Enable:

- CloudTrail
- CloudWatch Logs

Retention:

Minimum 90 days.

---

# Access Control

Require:

- MFA
- SSO
- Role-based access

---

# Data Protection

Sensitive data must never be:

- Publicly accessible
- Stored unencrypted

---

# Evidence Collection

Maintain:

- Terraform State
- Pipeline Logs
- CloudTrail Logs

for audit purposes.

---

# Compliance Review

Agent should flag:

- Public resources
- Missing encryption
- Missing logging
- Excessive permissions