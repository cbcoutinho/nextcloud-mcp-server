resource "aws_ecs_cluster" "this" {
  name = var.name

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_cloudwatch_log_group" "this" {
  name              = "/ecs/${var.name}"
  retention_in_days = var.log_retention_days
}

resource "aws_security_group" "task" {
  name        = "${var.name}-task"
  description = "ECS task ENI for ${var.name}"
  vpc_id      = var.vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "task_from_alb" {
  security_group_id            = aws_security_group.task.id
  referenced_security_group_id = aws_security_group.alb.id
  from_port                    = var.container_port
  to_port                      = var.container_port
  ip_protocol                  = "tcp"
  description                  = "Container port from ALB"
}

resource "aws_vpc_security_group_egress_rule" "task_all_v4" {
  security_group_id = aws_security_group.task.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_vpc_security_group_egress_rule" "task_all_v6" {
  security_group_id = aws_security_group.task.id
  cidr_ipv6         = "::/0"
  ip_protocol       = "-1"
}

locals {
  container_name = var.name

  # ALLOWED_MCP_CLIENTS is appended only when non-empty; an empty value would
  # override the upstream default (claude-desktop, test-mcp-client) and lock
  # everyone out.
  container_env = concat(
    [
      { name = "ENABLE_LOGIN_FLOW", value = "true" },
      { name = "ENABLE_SEMANTIC_SEARCH", value = "true" },
      { name = "ENABLE_BACKGROUND_OPERATIONS", value = "true" },
      # Explicit collection name; otherwise upstream auto-derives one from the
      # task hostname, which churns the collection on every rolling deploy.
      { name = "QDRANT_COLLECTION", value = var.qdrant_collection },
      { name = "TOKEN_STORAGE_DB", value = "/app/data/tokens.db" },
      { name = "NEXTCLOUD_MCP_SERVER_URL", value = "https://${local.fqdn}" },
      { name = "OIDC_DISCOVERY_URL", value = "${var.nextcloud_url}/.well-known/openid-configuration" },
      { name = "AWS_REGION", value = data.aws_region.current.region },
      { name = "BEDROCK_EMBEDDING_MODEL", value = var.bedrock_embedding_model },
      { name = "VECTOR_SYNC_SCAN_INTERVAL", value = tostring(var.vector_sync_scan_interval) },
      { name = "VECTOR_SYNC_PROCESSOR_WORKERS", value = tostring(var.vector_sync_processor_workers) },
    ],
    var.use_external_qdrant ? [] : [
      { name = "QDRANT_URL", value = "http://qdrant.${aws_service_discovery_private_dns_namespace.this.name}:${local.qdrant_port}" },
    ],
    length(var.allowed_mcp_clients) > 0 ? [
      { name = "ALLOWED_MCP_CLIENTS", value = join(",", var.allowed_mcp_clients) },
    ] : [],
    var.allowed_mgmt_client != "" ? [
      { name = "ALLOWED_MGMT_CLIENT", value = var.allowed_mgmt_client },
    ] : [],
  )

  container_secrets = concat(
    [
      { name = "NEXTCLOUD_HOST", valueFrom = "${var.secret_arn}:host::" },
      { name = "NEXTCLOUD_OIDC_CLIENT_ID", valueFrom = "${var.secret_arn}:client_id::" },
      { name = "NEXTCLOUD_OIDC_CLIENT_SECRET", valueFrom = "${var.secret_arn}:client_secret::" },
      { name = "TOKEN_ENCRYPTION_KEY", valueFrom = "${var.secret_arn}:token_encryption_key::" },
      { name = "WEBHOOK_SECRET", valueFrom = "${var.secret_arn}:webhook_secret::" },
    ],
    var.use_external_qdrant ? [
      { name = "QDRANT_URL", valueFrom = "${var.secret_arn}:qdrant_url::" },
      { name = "QDRANT_API_KEY", valueFrom = "${var.secret_arn}:qdrant_api_key::" },
    ] : [],
  )
}

resource "aws_ecs_task_definition" "this" {
  family                   = var.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = tostring(var.cpu)
  memory                   = tostring(var.memory)
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  volume {
    name = "data"
    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.this.id
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = aws_efs_access_point.data.id
        iam             = "ENABLED"
      }
    }
  }

  container_definitions = jsonencode([
    {
      name      = local.container_name
      image     = "${var.image}:${var.image_tag}"
      essential = true

      command = [
        "--transport", "streamable-http",
        "--oauth",
        "--port", tostring(var.container_port),
      ]

      portMappings = [
        {
          containerPort = var.container_port
          protocol      = "tcp"
        },
      ]

      environment = local.container_env
      secrets     = local.container_secrets

      mountPoints = [
        { sourceVolume = "data", containerPath = "/app/data", readOnly = false },
      ]

      healthCheck = {
        command     = ["CMD-SHELL", "curl -fsS http://localhost:${var.container_port}/health/live || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 60
      }

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.this.name
          awslogs-region        = data.aws_region.current.region
          awslogs-stream-prefix = "ecs"
        }
      }
    },
  ])
}

resource "aws_ecs_service" "this" {
  name            = var.name
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.this.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  availability_zone_rebalancing      = "ENABLED"

  enable_execute_command = true

  network_configuration {
    subnets          = var.public_subnet_ids
    security_groups  = [aws_security_group.task.id]
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.this.arn
    container_name   = local.container_name
    container_port   = var.container_port
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  # The listener must exist before the service registers targets.
  depends_on = [
    aws_lb_listener.https,
    aws_efs_mount_target.this,
  ]
}
