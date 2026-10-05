# Existing Repository Structure Compliance

## Primary Rule

Before generating any Terraform code, the agent must inspect the existing repository structure.

If Terraform code, modules, naming conventions, or deployment patterns already exist in the repository, the agent must follow the existing repository standards instead of creating a new structure.

Repository conventions always take precedence over generic Terraform standards.

---

## Repository Analysis Requirements

Before generating code, the agent must analyze:

- Existing folder structure
- Module structure
- Naming conventions
- Variable naming standards
- Tagging standards
- Backend configuration
- Provider configuration
- Environment structure
- Terragrunt structure (if present)
- CI/CD pipeline expectations
- Existing code patterns

---

## Existing Pattern Detection

### Example 1: Resource-Specific Files

If the repository already contains:

```text
terraform/
├── networking.tf
├── security.tf
├── compute.tf
└── outputs.tf
```

New resources must be added to the appropriate existing files.

Do NOT create:

```text
vpc.tf
ec2.tf
alb.tf
```

unless that pattern already exists.

---

### Example 2: Service-Based Structure

If the repository already contains:

```text
terraform/
├── vpc/
├── eks/
├── rds/
└── shared-services/
```

The agent must follow the same service-based organization.

Do not introduce a different structure.

---

### Example 3: Terragrunt Repository

If the repository uses Terragrunt:

```text
live/
├── dev/
├── qa/
└── prod/

modules/
```

The agent must generate:

- terragrunt.hcl files
- Terraform modules
- Environment-specific configurations

The agent must not generate a standalone Terraform project structure.

---

### Example 4: Monorepo Structure

If infrastructure is organized as:

```text
infra/
├── aws/
├── azure/
├── shared/
```

New code must follow the same structure.

Do not reorganize the repository.

---

## File Placement Rules

When adding new infrastructure:

1. Reuse existing files whenever appropriate.
2. Reuse existing modules whenever possible.
3. Extend existing variables.tf files.
4. Extend existing outputs.tf files.
5. Follow existing folder hierarchy.
6. Follow existing naming conventions.

Do not create duplicate patterns.

---

## Module Reuse Policy

Before creating a new module, the agent must check whether a similar module already exists.

Example:

Existing repository:

```text
modules/
├── vpc/
├── s3/
├── security-group/
```

If a VPC is requested:

✅ Reuse existing VPC module

❌ Create another VPC module

---

## Naming Convention Compliance

The agent must discover naming patterns from existing resources.

Example:

Existing resources:

```text
iris-dev-vpc
iris-dev-alb
iris-dev-rds
```

Generated resources should follow:

```text
iris-dev-eks
iris-dev-cache
```

Do not introduce a new naming convention.

---

## Tagging Compliance

If repository tags follow:

```hcl
tags = {
  Environment = var.environment
  Product     = var.product
  Owner       = var.owner
}
```

The agent must reuse the same tagging structure.

Do not create a different tagging model.

---

## Existing Backend Configuration

If backend configuration already exists:

```hcl
terraform {
  backend "s3" {}
}
```

Reuse the existing backend approach.

Do not introduce a new backend type.

---

## Existing Provider Configuration

If provider aliases or multi-account configurations already exist, reuse them.

Example:

```hcl
provider "aws" {
  alias = "shared"
}

provider "aws" {
  alias = "workload"
}
```

The agent must integrate with the existing provider strategy.

---

## Repository Precedence Order

The agent must follow this precedence:

1. Existing repository conventions
2. Existing module structure
3. Existing environment structure
4. Existing naming standards
5. Existing tagging standards
6. Existing CI/CD requirements
7. Terraform Code Generation Standards (this document)

Repository standards always override generic standards.

---

## Agent Mandatory Rule

Before generating any Terraform code:

1. Analyze repository structure.
2. Identify existing patterns.
3. Reuse existing modules.
4. Reuse existing files.
5. Follow existing conventions.
6. Only create new structures if no existing pattern exists.

The objective is to make generated code indistinguishable from code written by engineers already maintaining the repository.