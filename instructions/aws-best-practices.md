# AWS Best Practices

## VPC Design

Use:

- Multi-AZ architecture
- Private subnets for workloads
- Public subnets only for ingress components

Recommended:

3 AZ minimum for production.

---

# S3

Enable:

- Versioning
- Encryption
- Block Public Access

Prefer:

- Bucket Policies

Avoid:

- Public buckets

---

# RDS

Enable:

- Multi-AZ
- Automated backups
- Enhanced Monitoring
- Encryption

Prefer:

- Private subnet deployment

---

# ECS

Enable:

- Autoscaling
- CloudWatch logging

Prefer:

- Fargate where suitable

---

# EKS

Enable:

- IRSA
- Cluster Autoscaler or Karpenter
- Private API endpoint where possible

Avoid:

- Cluster admin access for applications

---

# IAM

Use:

- Roles
- OIDC

Avoid:

- Access keys

---

# Monitoring

Mandatory:

- CloudWatch
- CloudTrail
- AWS Config
- GuardDuty

---

# Backup

Use:

- AWS Backup

Define:

- RPO
- RTO