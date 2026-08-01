resource "aws_security_group" "efs" {
  name        = "${var.name}-efs"
  description = "NFS ingress from ${var.name} ECS tasks"
  vpc_id      = var.vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "efs_from_task" {
  security_group_id            = aws_security_group.efs.id
  referenced_security_group_id = aws_security_group.task.id
  from_port                    = 2049
  to_port                      = 2049
  ip_protocol                  = "tcp"
  description                  = "NFS from task"
}

# EFS mount targets accept NFS from any SG referenced by the EFS SG. The
# mcp-server task SG covers itself; the qdrant task SG (when present) gets
# its own ingress rule.
resource "aws_vpc_security_group_ingress_rule" "efs_from_qdrant" {
  count                        = var.use_external_qdrant ? 0 : 1
  security_group_id            = aws_security_group.efs.id
  referenced_security_group_id = aws_security_group.qdrant[0].id
  from_port                    = 2049
  to_port                      = 2049
  ip_protocol                  = "tcp"
  description                  = "NFS from qdrant task"
}

resource "aws_efs_file_system" "this" {
  encrypted        = true
  performance_mode = "generalPurpose"
  throughput_mode  = "bursting"

  lifecycle_policy {
    transition_to_ia = "AFTER_30_DAYS"
  }

  tags = {
    Name = var.name
  }
}

resource "aws_efs_mount_target" "this" {
  for_each = toset(var.private_subnet_ids)

  file_system_id  = aws_efs_file_system.this.id
  subnet_id       = each.value
  security_groups = [aws_security_group.efs.id]
}

# These access points deliberately stay on uid/gid 0 even though the image now
# runs as uid 1000 (Dockerfile `USER 1000:0`). An access point with posix_user
# set enforces that uid/gid for ALL I/O through it regardless of the container's
# own uid, so the task keeps working unchanged and files stay consistently
# owned. Changing posix_user forces access-point replacement, and creation_info
# only applies when the root directory is first created -- existing files would
# keep uid 0 -- so moving off root here needs a one-shot chown, not just an edit.
resource "aws_efs_access_point" "data" {
  file_system_id = aws_efs_file_system.this.id

  posix_user {
    uid = 0
    gid = 0
  }

  root_directory {
    path = "/data"
    creation_info {
      owner_uid   = 0
      owner_gid   = 0
      permissions = "0755"
    }
  }

  tags = {
    Name = "${var.name}-data"
  }
}

# Qdrant's image runs as root (debian-slim base, no USER directive), matching
# the other access points above. Mounted at /qdrant/storage which is qdrant's
# default storage_path.
resource "aws_efs_access_point" "qdrant" {
  count          = var.use_external_qdrant ? 0 : 1
  file_system_id = aws_efs_file_system.this.id

  posix_user {
    uid = 0
    gid = 0
  }

  root_directory {
    path = "/qdrant"
    creation_info {
      owner_uid   = 0
      owner_gid   = 0
      permissions = "0755"
    }
  }

  tags = {
    Name = "${var.name}-qdrant"
  }
}
