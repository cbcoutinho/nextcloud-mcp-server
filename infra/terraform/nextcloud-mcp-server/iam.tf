data "aws_iam_policy_document" "ecs_tasks_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

###
# Execution role — used by the ECS agent to pull the image, read secrets,
# and ship logs. Does not have application-level permissions.

resource "aws_iam_role" "execution" {
  name               = "${var.name}-execution"
  path               = "/ecs/"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution_secrets" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [var.secret_arn]
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  name   = "secrets-read"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_secrets.json
}

###
# Task role — used by the running container. Needs Bedrock for embeddings
# and EFS client access to the two access points.

resource "aws_iam_role" "task" {
  name               = "${var.name}-task"
  path               = "/ecs/"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

data "aws_iam_policy_document" "task_bedrock" {
  statement {
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [data.aws_region.current.region]
    }
  }
}

resource "aws_iam_role_policy" "task_bedrock" {
  name   = "bedrock-invoke"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task_bedrock.json
}

data "aws_iam_policy_document" "task_efs" {
  statement {
    actions = [
      "elasticfilesystem:ClientMount",
      "elasticfilesystem:ClientWrite",
      "elasticfilesystem:ClientRootAccess",
    ]
    resources = [aws_efs_file_system.this.arn]
    condition {
      test     = "StringEquals"
      variable = "elasticfilesystem:AccessPointArn"
      values = [
        aws_efs_access_point.data.arn,
      ]
    }
  }
}

resource "aws_iam_role_policy" "task_efs" {
  name   = "efs-client"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task_efs.json
}

# ECS Exec support — lets us `aws ecs execute-command` into a running task
# for debugging without SSH.
data "aws_iam_policy_document" "task_exec" {
  statement {
    actions = [
      "ssmmessages:CreateControlChannel",
      "ssmmessages:CreateDataChannel",
      "ssmmessages:OpenControlChannel",
      "ssmmessages:OpenDataChannel",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "task_exec" {
  name   = "ecs-exec"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task_exec.json
}

###
# Qdrant task role — only needs EFS access to its own access point and
# ECS Exec for debugging. Reuses the shared execution role.

resource "aws_iam_role" "qdrant_task" {
  count              = var.use_external_qdrant ? 0 : 1
  name               = "${local.qdrant_name}-task"
  path               = "/ecs/"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

data "aws_iam_policy_document" "qdrant_task_efs" {
  count = var.use_external_qdrant ? 0 : 1
  statement {
    actions = [
      "elasticfilesystem:ClientMount",
      "elasticfilesystem:ClientWrite",
      "elasticfilesystem:ClientRootAccess",
    ]
    resources = [aws_efs_file_system.this.arn]
    condition {
      test     = "StringEquals"
      variable = "elasticfilesystem:AccessPointArn"
      values   = [aws_efs_access_point.qdrant[0].arn]
    }
  }
}

resource "aws_iam_role_policy" "qdrant_task_efs" {
  count  = var.use_external_qdrant ? 0 : 1
  name   = "efs-client"
  role   = aws_iam_role.qdrant_task[0].id
  policy = data.aws_iam_policy_document.qdrant_task_efs[0].json
}

resource "aws_iam_role_policy" "qdrant_task_exec" {
  count  = var.use_external_qdrant ? 0 : 1
  name   = "ecs-exec"
  role   = aws_iam_role.qdrant_task[0].id
  policy = data.aws_iam_policy_document.task_exec.json
}
