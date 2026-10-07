terraform {
  required_version = ">= 1.9"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.6"
    }
    docker = {
      source  = "kreuzwerker/docker"
      version = ">= 3.0, < 5.0"
    }
  }
}
