provider "aws" {
  region  = var.region
  profile = var.aws_profile
  default_tags {
    tags = {
      Project   = local.name
      ManagedBy = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_ecr_authorization_token" "this" {}

# Builds the app image locally and pushes it to ECR, so one `terraform apply` deploys code too.
provider "docker" {
  host = var.docker_host
  registry_auth {
    address  = replace(data.aws_ecr_authorization_token.this.proxy_endpoint, "https://", "")
    username = data.aws_ecr_authorization_token.this.user_name
    password = data.aws_ecr_authorization_token.this.password
  }
}
