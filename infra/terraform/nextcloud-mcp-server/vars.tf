variable "name" {
  description = "Logical name prefix for resources"
  type        = string
  default     = "nextcloud-mcp-server"
}

variable "vpc_id" {
  description = "VPC ID to deploy into"
  type        = string
}

variable "public_subnet_ids" {
  description = "Public subnet IDs (for the ALB and the ECS task ENI). Tasks run with assign_public_ip=true since this VPC has no NAT gateway; the task SG only allows ingress from the ALB SG."
  type        = list(string)
}

variable "private_subnet_ids" {
  description = "Private subnet IDs (for EFS mount targets only)."
  type        = list(string)
}

variable "nextcloud_url" {
  description = "Public URL of the Nextcloud instance the MCP server pairs with (e.g., https://cloud.example.com). Used to advertise the OIDC discovery endpoint via /api/v1/status so the astrolabe Nextcloud app can discover Nextcloud's oidc_provider as the IdP instead of falling back to http://localhost."
  type        = string

  validation {
    condition     = startswith(var.nextcloud_url, "https://")
    error_message = "nextcloud_url must include the https:// scheme; OIDC discovery is composed by appending /.well-known/openid-configuration."
  }
}

variable "zone_id" {
  description = "Route53 hosted zone ID for the random subdomain"
  type        = string
}

variable "zone_name" {
  description = "Route53 hosted zone name (without trailing dot), e.g. astrolabeonline.com"
  type        = string
}

variable "image" {
  description = "Container image (without tag)"
  type        = string
  default     = "ghcr.io/cbcoutinho/nextcloud-mcp-server"
}

variable "image_tag" {
  description = "Container image tag. Pin to a specific release; avoid :latest."
  type        = string
}

variable "secret_arn" {
  description = "ARN of the Secrets Manager secret holding JSON {host, client_id, client_secret, token_encryption_key, webhook_secret}"
  type        = string
}

variable "allowed_mcp_clients" {
  description = "MCP OAuth client allowlist published as ALLOWED_MCP_CLIENTS. Each entry is `id` or `id|redirect_uri`. Empty list keeps the upstream defaults (claude-desktop, test-mcp-client)."
  type        = list(string)
  default     = []
}

variable "allowed_mgmt_client" {
  description = "Management API client allowlist published as ALLOWED_MGMT_CLIENT (comma-separated client IDs). Required from upstream v0.74.0+: when unset/empty the management API is fail-closed and rejects all tokens. Empty string skips publishing the env var."
  type        = string
  default     = ""
}

variable "bedrock_embedding_model" {
  description = "Bedrock model ID used for semantic search embeddings"
  type        = string
  default     = "amazon.titan-embed-text-v2:0"
}

variable "container_port" {
  description = "Port the server listens on inside the container"
  type        = number
  default     = 8004
}

variable "cpu" {
  description = "Fargate task vCPU units (1024 = 1 vCPU)"
  type        = number
  default     = 512
}

variable "memory" {
  description = "Fargate task memory (MiB)"
  type        = number
  default     = 1024
}

variable "log_retention_days" {
  description = "CloudWatch log retention in days"
  type        = number
  default     = 30
}

variable "vector_sync_scan_interval" {
  description = "Seconds between background vector sync scans"
  type        = number
  default     = 60
}

variable "vector_sync_processor_workers" {
  description = "Concurrent embedding workers. Keep at 1 unless you've verified Bedrock quota headroom."
  type        = number
  default     = 1
}

variable "qdrant_image" {
  description = "Qdrant container image (without tag)"
  type        = string
  default     = "qdrant/qdrant"
}

variable "qdrant_collection" {
  description = "Qdrant collection name. Set to a stable value (anything other than upstream's default 'nextcloud_content') so the upstream config doesn't fall through to its hostname-based auto-naming, which churns the collection on every rolling deploy."
  type        = string
  default     = "nextcloud-mcp"
}

variable "qdrant_image_tag" {
  description = "Qdrant container image tag (e.g., v1.15.0). Pin to a specific release; avoid :latest. Required only when use_external_qdrant = false; omit (or pass null) when use_external_qdrant = true."
  type        = string
  nullable    = true
  default     = null

  validation {
    condition     = var.use_external_qdrant || var.qdrant_image_tag != null
    error_message = "qdrant_image_tag is required when use_external_qdrant = false."
  }
}

variable "use_external_qdrant" {
  description = "When true, skip the in-AWS Qdrant ECS task and source QDRANT_URL/QDRANT_API_KEY from the Secrets Manager secret (keys: qdrant_url, qdrant_api_key). When false, run an in-AWS Qdrant Fargate task and point the MCP server at it via Cloud Map DNS."
  type        = bool
  default     = false
}

variable "qdrant_cpu" {
  description = "Qdrant Fargate task vCPU units (1024 = 1 vCPU)"
  type        = number
  default     = 512
}

variable "qdrant_memory" {
  description = "Qdrant Fargate task memory (MiB)"
  type        = number
  default     = 1024
}

variable "access_logs_bucket" {
  description = "S3 bucket for ALB access logs (must allow ELB log delivery). null disables access logging."
  type        = string
  default     = null
}
