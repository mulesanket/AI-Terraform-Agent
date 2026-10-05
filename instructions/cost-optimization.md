# Cost Optimization Standards

## Objective

Infrastructure should be cost-efficient by default.

---

# Compute

Prefer:

- Graviton instances
- Spot Instances where suitable
- Auto Scaling

Avoid:

- Oversized instances
- Idle environments

---

# EC2

Review:

- CPU utilization
- Memory utilization

Target:

40%-70% utilization

---

# RDS

Enable:

- Storage autoscaling

Prefer:

- Reserved Instances for production

Avoid:

- Overprovisioned databases

---

# S3

Use lifecycle policies.

Move data to:

- IA
- Glacier
- Deep Archive

when appropriate.

---

# ECS/EKS

Use:

- Cluster Autoscaler
- Karpenter
- HPA

Avoid static scaling.

---

# Data Transfer

Minimize:

- Cross-region traffic
- NAT Gateway usage

Prefer:

- VPC Endpoints

---

# Cost Review

Evaluate:

- Idle resources
- Unattached volumes
- Old snapshots
- Unused load balancers
- Overprovisioned databases

Generate recommendations.