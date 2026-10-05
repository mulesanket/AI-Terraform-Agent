# Terraform Module Development Standards

## Objective

Modules must be reusable, maintainable and versioned.

---

# Structure

Required:

modules/

vpc/

main.tf

variables.tf

outputs.tf

versions.tf

README.md

---

# Inputs

All configurable values must be variables.

Avoid hardcoding.

---

# Outputs

Expose only required outputs.

Avoid exposing sensitive values.

---

# Versioning

Pin provider versions.

Use semantic versioning.

Example:

v1.0.0

v1.1.0

v2.0.0

---

# Documentation

Each module must include:

- Purpose
- Inputs
- Outputs
- Examples

---

# Security

Do not embed:

- Secrets
- Credentials

inside modules.

---

# Testing

Validate using:

terraform validate

terraform fmt

tflint

checkov