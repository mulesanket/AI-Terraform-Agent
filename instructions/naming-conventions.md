# Naming Conventions

## Objective

Resources must be consistently named across environments.

---

# General Format

{project}-{environment}-{resource}

Examples:

schoolspider-prod-vpc

schoolspider-dev-rds

schoolspider-uat-alb

---

# Environment Names

Allowed:

- dev
- test
- qa
- uat
- stage
- prod

Avoid custom abbreviations.

---

# Resource Naming

## VPC

{project}-{env}-vpc

Example:

schoolspider-prod-vpc

---

## Subnets

{project}-{env}-{type}-{az}

Examples:

schoolspider-prod-private-euw2a

schoolspider-prod-public-euw2b

---

## Security Groups

{project}-{env}-{service}-sg

Example:

schoolspider-prod-rds-sg

---

## ECS

{project}-{env}-{service}

Example:

schoolspider-prod-api

---

## EKS

{project}-{env}-eks

---

## RDS

{project}-{env}-{engine}

Example:

schoolspider-prod-postgres

---

# Tags

Mandatory Tags

Environment
Project
Owner
CostCenter
ManagedBy
Terraform
BusinessUnit

Example

Environment = prod
Project = schoolspider
ManagedBy = terraform