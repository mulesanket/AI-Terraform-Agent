terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

resource "google_storage_bucket" "example" {
  name                        = "ai-terraform-agent-${var.suffix}"
  location                    = var.region
  force_destroy               = false
  uniform_bucket_level_access = true

  versioning {
    enabled = true
  }

  encryption {
    default_kms_key_name = ""  # Uses Google-managed encryption by default
  }

  labels = {
    environment = var.environment
    owner       = "infra-team"
    managed_by  = "terraform"
  }
}

variable "project_id" {
  type        = string
  description = "GCP project ID"
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "suffix" {
  type    = string
  default = "demo"
}

output "bucket_url" {
  value = google_storage_bucket.example.url
}
