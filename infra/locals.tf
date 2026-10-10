locals {
  name             = "collectionsinference"
  metric_namespace = "CollectionsChallenge"
  service_name     = "voice-agent"
  account_id       = data.aws_caller_identity.current.account_id
  status_index     = "by_status"
  provider_url     = "http://${aws_lb.models.dns_name}:8080/v1"
}
