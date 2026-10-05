# Network Architecture Standards

## CIDR Planning

Avoid overlapping CIDRs.

Recommended:

10.0.0.0/16
10.1.0.0/16
10.2.0.0/16

Reserve future growth ranges.

---

# Subnet Design

Minimum:

Public
Private
Database

per Availability Zone.

Example:

10.0.0.0/24 Public

10.0.10.0/24 Private

10.0.20.0/24 Database

---

# Routing

Public:

Internet Gateway

Private:

NAT Gateway

Database:

No internet route

---

# VPC Peering

Rules:

No overlapping CIDRs.

Document route propagation.

---

# Transit Gateway

Preferred over complex peering meshes.

Use for:

Multi-account architecture.

---

# Security

All east-west traffic must be controlled using:

- Security Groups
- Network ACLs where required

---

# DNS

Use:

- Route53 Private Hosted Zones

for internal services.