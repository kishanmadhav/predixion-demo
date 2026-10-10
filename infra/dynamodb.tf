resource "aws_dynamodb_table" "state" {
  name         = "${local.name}-state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }
  attribute {
    name = "gsi1pk"
    type = "S"
  }
  attribute {
    name = "gsi1sk"
    type = "S"
  }

  global_secondary_index {
    name      = local.status_index
    hash_key  = "gsi1pk"
    range_key = "gsi1sk"
    # Small bookkeeping attributes only, never the audio (payload/request) or results:
    # counting and listing dead letters must not read them. Same list as INDEX_ATTRIBUTES
    # in src/voice_agent/store/dynamodb.py (tests/store/test_dynamodb.py checks).
    projection_type = "INCLUDE"
    non_key_attributes = [
      "id", "call_id", "turn_id", "status", "reason", "failed_stage", "error_kind",
      "replay_count", "created_at", "claimed_at", "updated_at",
    ]
  }

  point_in_time_recovery {
    enabled = true
  }
  server_side_encryption {
    enabled = true
  }
}
