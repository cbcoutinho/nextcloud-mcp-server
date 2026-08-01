<!-- BEGIN_TF_DOCS -->
## Requirements

| Name | Version |
| ---- | ------- |
| <a name="requirement_terraform"></a> [terraform](#requirement\_terraform) | >= 1.9 |
| <a name="requirement_aws"></a> [aws](#requirement\_aws) | ~> 6.0 |
| <a name="requirement_random"></a> [random](#requirement\_random) | ~> 3.6 |

## Providers

| Name | Version |
| ---- | ------- |
| <a name="provider_aws"></a> [aws](#provider\_aws) | ~> 6.0 |
| <a name="provider_random"></a> [random](#provider\_random) | ~> 3.6 |

## Modules

No modules.

## Resources

| Name | Type |
| ---- | ---- |
| [aws_acm_certificate.this](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/acm_certificate) | resource |
| [aws_acm_certificate_validation.this](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/acm_certificate_validation) | resource |
| [aws_cloudwatch_log_group.qdrant](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_log_group) | resource |
| [aws_cloudwatch_log_group.this](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_log_group) | resource |
| [aws_ecs_cluster.this](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/ecs_cluster) | resource |
| [aws_ecs_service.qdrant](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/ecs_service) | resource |
| [aws_ecs_service.this](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/ecs_service) | resource |
| [aws_ecs_task_definition.qdrant](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/ecs_task_definition) | resource |
| [aws_ecs_task_definition.this](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/ecs_task_definition) | resource |
| [aws_efs_access_point.data](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/efs_access_point) | resource |
| [aws_efs_access_point.qdrant](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/efs_access_point) | resource |
| [aws_efs_file_system.this](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/efs_file_system) | resource |
| [aws_efs_mount_target.this](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/efs_mount_target) | resource |
| [aws_iam_role.execution](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role) | resource |
| [aws_iam_role.qdrant_task](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role) | resource |
| [aws_iam_role.task](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role) | resource |
| [aws_iam_role_policy.execution_secrets](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role_policy) | resource |
| [aws_iam_role_policy.qdrant_task_efs](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role_policy) | resource |
| [aws_iam_role_policy.qdrant_task_exec](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role_policy) | resource |
| [aws_iam_role_policy.task_bedrock](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role_policy) | resource |
| [aws_iam_role_policy.task_efs](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role_policy) | resource |
| [aws_iam_role_policy.task_exec](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role_policy) | resource |
| [aws_iam_role_policy_attachment.execution_managed](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role_policy_attachment) | resource |
| [aws_lb.this](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/lb) | resource |
| [aws_lb_listener.http_redirect](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/lb_listener) | resource |
| [aws_lb_listener.https](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/lb_listener) | resource |
| [aws_lb_target_group.this](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/lb_target_group) | resource |
| [aws_route53_record.alias](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/route53_record) | resource |
| [aws_route53_record.cert_validation](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/route53_record) | resource |
| [aws_security_group.alb](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/security_group) | resource |
| [aws_security_group.efs](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/security_group) | resource |
| [aws_security_group.qdrant](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/security_group) | resource |
| [aws_security_group.task](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/security_group) | resource |
| [aws_service_discovery_private_dns_namespace.this](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/service_discovery_private_dns_namespace) | resource |
| [aws_service_discovery_service.qdrant](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/service_discovery_service) | resource |
| [aws_vpc_security_group_egress_rule.alb_all_v4](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/vpc_security_group_egress_rule) | resource |
| [aws_vpc_security_group_egress_rule.alb_all_v6](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/vpc_security_group_egress_rule) | resource |
| [aws_vpc_security_group_egress_rule.qdrant_all_v4](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/vpc_security_group_egress_rule) | resource |
| [aws_vpc_security_group_egress_rule.qdrant_all_v6](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/vpc_security_group_egress_rule) | resource |
| [aws_vpc_security_group_egress_rule.task_all_v4](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/vpc_security_group_egress_rule) | resource |
| [aws_vpc_security_group_egress_rule.task_all_v6](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/vpc_security_group_egress_rule) | resource |
| [aws_vpc_security_group_ingress_rule.alb_http_v4](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/vpc_security_group_ingress_rule) | resource |
| [aws_vpc_security_group_ingress_rule.alb_http_v6](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/vpc_security_group_ingress_rule) | resource |
| [aws_vpc_security_group_ingress_rule.alb_https_v4](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/vpc_security_group_ingress_rule) | resource |
| [aws_vpc_security_group_ingress_rule.alb_https_v6](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/vpc_security_group_ingress_rule) | resource |
| [aws_vpc_security_group_ingress_rule.efs_from_qdrant](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/vpc_security_group_ingress_rule) | resource |
| [aws_vpc_security_group_ingress_rule.efs_from_task](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/vpc_security_group_ingress_rule) | resource |
| [aws_vpc_security_group_ingress_rule.qdrant_from_task](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/vpc_security_group_ingress_rule) | resource |
| [aws_vpc_security_group_ingress_rule.task_from_alb](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/vpc_security_group_ingress_rule) | resource |
| [random_pet.subdomain](https://registry.terraform.io/providers/hashicorp/random/latest/docs/resources/pet) | resource |
| [aws_caller_identity.current](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/caller_identity) | data source |
| [aws_iam_policy_document.ecs_tasks_trust](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_policy_document) | data source |
| [aws_iam_policy_document.execution_secrets](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_policy_document) | data source |
| [aws_iam_policy_document.qdrant_task_efs](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_policy_document) | data source |
| [aws_iam_policy_document.task_bedrock](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_policy_document) | data source |
| [aws_iam_policy_document.task_efs](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_policy_document) | data source |
| [aws_iam_policy_document.task_exec](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_policy_document) | data source |
| [aws_region.current](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/region) | data source |

## Inputs

| Name | Description | Type | Default | Required |
| ---- | ----------- | ---- | ------- | :------: |
| <a name="input_allowed_mcp_clients"></a> [allowed\_mcp\_clients](#input\_allowed\_mcp\_clients) | MCP OAuth client allowlist published as ALLOWED\_MCP\_CLIENTS. Each entry is `id` or `id|redirect_uri`. Empty list keeps the upstream defaults (claude-desktop, test-mcp-client). | `list(string)` | `[]` | no |
| <a name="input_allowed_mgmt_client"></a> [allowed\_mgmt\_client](#input\_allowed\_mgmt\_client) | Management API client allowlist published as ALLOWED\_MGMT\_CLIENT (comma-separated client IDs). Required from upstream v0.74.0+: when unset/empty the management API is fail-closed and rejects all tokens. Empty string skips publishing the env var. | `string` | `""` | no |
| <a name="input_bedrock_embedding_model"></a> [bedrock\_embedding\_model](#input\_bedrock\_embedding\_model) | Bedrock model ID used for semantic search embeddings | `string` | `"amazon.titan-embed-text-v2:0"` | no |
| <a name="input_container_port"></a> [container\_port](#input\_container\_port) | Port the server listens on inside the container | `number` | `8004` | no |
| <a name="input_cpu"></a> [cpu](#input\_cpu) | Fargate task vCPU units (1024 = 1 vCPU) | `number` | `512` | no |
| <a name="input_image"></a> [image](#input\_image) | Container image (without tag) | `string` | `"ghcr.io/cbcoutinho/nextcloud-mcp-server"` | no |
| <a name="input_image_tag"></a> [image\_tag](#input\_image\_tag) | Container image tag. Pin to a specific release; avoid :latest. | `string` | n/a | yes |
| <a name="input_log_retention_days"></a> [log\_retention\_days](#input\_log\_retention\_days) | CloudWatch log retention in days | `number` | `30` | no |
| <a name="input_memory"></a> [memory](#input\_memory) | Fargate task memory (MiB) | `number` | `1024` | no |
| <a name="input_name"></a> [name](#input\_name) | Logical name prefix for resources | `string` | `"nextcloud-mcp-server"` | no |
| <a name="input_nextcloud_url"></a> [nextcloud\_url](#input\_nextcloud\_url) | Public URL of the Nextcloud instance the MCP server pairs with (e.g., https://cloud.example.com). Used to advertise the OIDC discovery endpoint via /api/v1/status so the astrolabe Nextcloud app can discover Nextcloud's oidc\_provider as the IdP instead of falling back to http://localhost. | `string` | n/a | yes |
| <a name="input_private_subnet_ids"></a> [private\_subnet\_ids](#input\_private\_subnet\_ids) | Private subnet IDs (for EFS mount targets only). | `list(string)` | n/a | yes |
| <a name="input_public_subnet_ids"></a> [public\_subnet\_ids](#input\_public\_subnet\_ids) | Public subnet IDs (for the ALB and the ECS task ENI). Tasks run with assign\_public\_ip=true since this VPC has no NAT gateway; the task SG only allows ingress from the ALB SG. | `list(string)` | n/a | yes |
| <a name="input_qdrant_collection"></a> [qdrant\_collection](#input\_qdrant\_collection) | Qdrant collection name. Set to a stable value (anything other than upstream's default 'nextcloud\_content') so the upstream config doesn't fall through to its hostname-based auto-naming, which churns the collection on every rolling deploy. | `string` | `"nextcloud-mcp"` | no |
| <a name="input_qdrant_cpu"></a> [qdrant\_cpu](#input\_qdrant\_cpu) | Qdrant Fargate task vCPU units (1024 = 1 vCPU) | `number` | `512` | no |
| <a name="input_qdrant_image"></a> [qdrant\_image](#input\_qdrant\_image) | Qdrant container image (without tag) | `string` | `"qdrant/qdrant"` | no |
| <a name="input_qdrant_image_tag"></a> [qdrant\_image\_tag](#input\_qdrant\_image\_tag) | Qdrant container image tag (e.g., v1.15.0). Pin to a specific release; avoid :latest. Required only when use\_external\_qdrant = false; omit (or pass null) when use\_external\_qdrant = true. | `string` | `null` | no |
| <a name="input_qdrant_memory"></a> [qdrant\_memory](#input\_qdrant\_memory) | Qdrant Fargate task memory (MiB) | `number` | `1024` | no |
| <a name="input_secret_arn"></a> [secret\_arn](#input\_secret\_arn) | ARN of the Secrets Manager secret holding JSON {host, client\_id, client\_secret, token\_encryption\_key, webhook\_secret} | `string` | n/a | yes |
| <a name="input_use_external_qdrant"></a> [use\_external\_qdrant](#input\_use\_external\_qdrant) | When true, skip the in-AWS Qdrant ECS task and source QDRANT\_URL/QDRANT\_API\_KEY from the Secrets Manager secret (keys: qdrant\_url, qdrant\_api\_key). When false, run an in-AWS Qdrant Fargate task and point the MCP server at it via Cloud Map DNS. | `bool` | `false` | no |
| <a name="input_vector_sync_processor_workers"></a> [vector\_sync\_processor\_workers](#input\_vector\_sync\_processor\_workers) | Concurrent embedding workers. Keep at 1 unless you've verified Bedrock quota headroom. | `number` | `1` | no |
| <a name="input_vector_sync_scan_interval"></a> [vector\_sync\_scan\_interval](#input\_vector\_sync\_scan\_interval) | Seconds between background vector sync scans | `number` | `60` | no |
| <a name="input_vpc_id"></a> [vpc\_id](#input\_vpc\_id) | VPC ID to deploy into | `string` | n/a | yes |
| <a name="input_zone_id"></a> [zone\_id](#input\_zone\_id) | Route53 hosted zone ID for the random subdomain | `string` | n/a | yes |
| <a name="input_zone_name"></a> [zone\_name](#input\_zone\_name) | Route53 hosted zone name (without trailing dot), e.g. astrolabeonline.com | `string` | n/a | yes |

## Outputs

| Name | Description |
| ---- | ----------- |
| <a name="output_alb_dns_name"></a> [alb\_dns\_name](#output\_alb\_dns\_name) | n/a |
| <a name="output_ecs_cluster_name"></a> [ecs\_cluster\_name](#output\_ecs\_cluster\_name) | n/a |
| <a name="output_ecs_service_name"></a> [ecs\_service\_name](#output\_ecs\_service\_name) | n/a |
| <a name="output_efs_id"></a> [efs\_id](#output\_efs\_id) | EFS file-system ID. Marked sensitive — surfacing it in CI logs invites enumeration of mount targets. |
| <a name="output_fqdn"></a> [fqdn](#output\_fqdn) | Fully-qualified domain name |
| <a name="output_log_group_name"></a> [log\_group\_name](#output\_log\_group\_name) | n/a |
| <a name="output_qdrant_dns_name"></a> [qdrant\_dns\_name](#output\_qdrant\_dns\_name) | Internal DNS name where mcp-server reaches qdrant (null when use\_external\_qdrant = true). |
| <a name="output_qdrant_service_name"></a> [qdrant\_service\_name](#output\_qdrant\_service\_name) | Qdrant ECS service name (null when use\_external\_qdrant = true) |
| <a name="output_subdomain"></a> [subdomain](#output\_subdomain) | Generated random subdomain (label only, without the zone) |
| <a name="output_task_role_arn"></a> [task\_role\_arn](#output\_task\_role\_arn) | Task role ARN. Marked sensitive — knowing the ARN is the first step to abusing it via SSRF/role-confusion. |
| <a name="output_url"></a> [url](#output\_url) | Public HTTPS URL of the MCP server |
<!-- END_TF_DOCS -->