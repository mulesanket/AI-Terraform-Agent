terraform {
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.0"
    }
  }
}

provider "azurerm" {
  features {}
}

resource "azurerm_resource_group" "example" {
  name     = "rg-ai-terraform-agent-${var.environment}"
  location = var.location

  tags = {
    Environment = var.environment
    Owner       = "infra-team"
    ManagedBy   = "terraform"
  }
}

resource "azurerm_storage_account" "example" {
  name                     = "aiterraformagent${var.suffix}"
  resource_group_name      = azurerm_resource_group.example.name
  location                 = azurerm_resource_group.example.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
  min_tls_version          = "TLS1_2"

  blob_properties {
    versioning_enabled = true
  }

  tags = {
    Environment = var.environment
    Owner       = "infra-team"
    ManagedBy   = "terraform"
  }
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "location" {
  type    = string
  default = "East US"
}

variable "suffix" {
  type    = string
  default = "demo"
}

output "storage_account_id" {
  value = azurerm_storage_account.example.id
}
