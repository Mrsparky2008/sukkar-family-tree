# ---------------------------------------------------------------------------
# One small machine that runs the bot and holds the database.
#
# Lightsail rather than anything serverless, for one reason: SQLite wants a
# real disk. App Runner has no persistent storage, Lambda has none, and SQLite
# over EFS has locking problems that surface exactly when two relatives submit
# at the same moment. Everything else about the design survives a later move —
# db.py is the only file that touches the database — so this is a starting
# point, not a commitment.
#
#   terraform init
#   terraform apply
#
# Then follow deploy/README.md to paste the bot token in.
# ---------------------------------------------------------------------------

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

variable "region" {
  description = "AWS region. Sydney by default — most of the family is in NSW."
  type        = string
  default     = "ap-southeast-2"
}

variable "name" {
  description = "Name for the instance and its backup bucket."
  type        = string
  default     = "family-tree"
}

variable "bundle" {
  description = <<-TEXT
    Lightsail size. nano_3_0 is 512 MB and about $5/month, which is ample:
    the bot is idle between messages and the whole database is a few
    megabytes. Move up only if you outgrow it, which you will not.
  TEXT
  type        = string
  default     = "nano_3_0"
}

variable "repository" {
  description = "Git repository to deploy."
  type        = string
  default     = "https://github.com/Mrsparky2008/sukkar-family-tree.git"
}

variable "branch" {
  description = "Branch to deploy."
  type        = string
  default     = "main"
}

variable "ssh_key_name" {
  description = <<-TEXT
    Name of an existing Lightsail key pair to log in with. Leave empty and
    Lightsail issues a default key you download from the console.
  TEXT
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------
# The machine
# ---------------------------------------------------------------------------

resource "aws_lightsail_instance" "bot" {
  name              = var.name
  availability_zone = "${var.region}a"
  blueprint_id      = "debian_12"
  bundle_id         = var.bundle
  key_pair_name     = var.ssh_key_name != "" ? var.ssh_key_name : null

  user_data = templatefile("${path.module}/setup.sh", {
    repository = var.repository
    branch     = var.branch
    bucket     = local.bucket_name
    region     = var.region
  })

  tags = {
    Project = var.name
  }
}

# The bot only makes outbound connections to Telegram, so nothing needs to
# reach it. SSH is the single exception, and only for the person deploying.
resource "aws_lightsail_instance_public_ports" "bot" {
  instance_name = aws_lightsail_instance.bot.name

  port_info {
    protocol  = "tcp"
    from_port = 22
    to_port   = 22
  }
}

resource "aws_lightsail_static_ip" "bot" {
  name = "${var.name}-ip"
}

resource "aws_lightsail_static_ip_attachment" "bot" {
  static_ip_name = aws_lightsail_static_ip.bot.name
  instance_name  = aws_lightsail_instance.bot.name
}

# ---------------------------------------------------------------------------
# Backups
#
# The database is one file. A nightly copy to S3, kept for a year, is the
# whole disaster plan — and it is the thing that means nobody is ever asked
# to send their relatives' names in a second time.
# ---------------------------------------------------------------------------

locals {
  bucket_name = "${var.name}-backups-${data.aws_caller_identity.current.account_id}"
}

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "backups" {
  bucket = local.bucket_name
}

resource "aws_s3_bucket_public_access_block" "backups" {
  bucket                  = aws_s3_bucket.backups.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "backups" {
  bucket = aws_s3_bucket.backups.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id
  rule {
    id     = "keep-a-year"
    status = "Enabled"
    filter {}
    expiration { days = 365 }
    noncurrent_version_expiration { noncurrent_days = 90 }
  }
}

# An IAM user for the instance to write backups with. Lightsail instances
# cannot take an IAM role, so this is the narrowest alternative: one key that
# can put objects in one bucket and do nothing else at all.
resource "aws_iam_user" "backup" {
  name = "${var.name}-backup"
}

resource "aws_iam_user_policy" "backup" {
  name = "${var.name}-backup"
  user = aws_iam_user.backup.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:PutObject"]
      Resource = "${aws_s3_bucket.backups.arn}/*"
    }]
  })
}

resource "aws_iam_access_key" "backup" {
  user = aws_iam_user.backup.name
}

# ---------------------------------------------------------------------------

output "ip" {
  description = "Log in with: ssh admin@<this address>"
  value       = aws_lightsail_static_ip.bot.ip_address
}

output "backup_bucket" {
  value = aws_s3_bucket.backups.bucket
}

output "next_step" {
  value = <<-TEXT

    The machine is up. One thing left:

      ssh admin@${aws_lightsail_static_ip.bot.ip_address}
      sudo nano /opt/family-tree/.env      # paste the bot token
      sudo systemctl restart family-tree

    Then message the bot. See deploy/README.md for the rest.
  TEXT
}

output "backup_credentials" {
  description = "Written into .env by setup.sh; shown here only for reference."
  sensitive   = true
  value = {
    AWS_ACCESS_KEY_ID     = aws_iam_access_key.backup.id
    AWS_SECRET_ACCESS_KEY = aws_iam_access_key.backup.secret
  }
}
