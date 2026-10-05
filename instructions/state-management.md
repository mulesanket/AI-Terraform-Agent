# Terraform State Management Standards

## Objective

Terraform state must be secure and reliable.

---

# Backend

Mandatory:

Remote backend

Preferred:

AWS S3 + DynamoDB

---

# State Locking

Enable:

DynamoDB locking

Never use local state in production.

---

# State Separation

Separate state by:

- Environment
- Account
- Region

Example:

prod/network

prod/eks

prod/shared-services

---

# Security

Enable:

- S3 Encryption
- Versioning
- Access Logging

Restrict access to state bucket.

---

# Recovery

Enable:

- Bucket Versioning

Allow state rollback.

---

# State Access

Only CI/CD pipelines and platform engineers should access state.

---

# Best Practices

Never:

- Modify state manually
- Commit tfstate files
- Share state files

Always:

- Use remote backend
- Lock state
- Backup state