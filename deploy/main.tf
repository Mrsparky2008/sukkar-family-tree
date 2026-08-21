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

variable "code_url" {
  description = "Presigned GET URL for the code bundle, so first boot needs only curl."
  type        = string
  default     = ""
  sensitive   = true
}

variable "log_url" {
  description = "Presigned PUT URL the boot log uploads itself to."
  type        = string
  default     = ""
  sensitive   = true
}

variable "telegram_bot_token" {
  description = <<-TEXT
    The bot token from @BotFather. Optional: leave empty and paste it into
    /opt/family-tree/.env over SSH instead. When set, it is written into .env
    at first boot — which also means it is visible in the instance's user
    data and in Terraform state, so use this path only when the person
    deploying cannot SSH (and rotate the token later if that bothers you).
  TEXT
  type      = string
  default   = ""
  sensitive = true
}

variable "admin_password" {
  description = "Password for the review interface. Optional, same trade as the token."
  type      = string
  default   = ""
  sensitive = true
}



# ---------------------------------------------------------------------------
# The machine
#
# Plain EC2, after Lightsail's launch scripts turned out never to execute in
# this account at all (proven with a three-line probe). EC2 also does this
# better: the instance gets an IAM role, so no AWS keys are ever stored on
# the box, and Systems Manager gives shell access through the AWS API with
# no SSH keys to manage.
# ---------------------------------------------------------------------------

variable "instance_type" {
  description = "1 GB of memory is comfortable; the bot itself is tiny."
  type        = string
  default     = "t4g.micro"
}

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-arm64-server-*"]
  }
}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

resource "aws_security_group" "bot" {
  name        = "${var.name}-bot"
  description = "Family tree bot: outbound only; shell access is via SSM"
  vpc_id      = data.aws_vpc.default.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_iam_role" "bot" {
  name = "${var.name}-bot"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "bot_s3" {
  name = "backups-and-code"
  role = aws_iam_role.bot.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "${aws_s3_bucket.backups.arn}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.backups.arn}/code/*"
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "bot_ssm" {
  role       = aws_iam_role.bot.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "bot" {
  name = "${var.name}-bot"
  role = aws_iam_role.bot.name
}

resource "aws_instance" "bot" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.instance_type
  subnet_id              = data.aws_subnets.default.ids[0]
  vpc_security_group_ids = [aws_security_group.bot.id]
  iam_instance_profile   = aws_iam_instance_profile.bot.name

  user_data = templatefile("${path.module}/setup.sh", {
    repository     = var.repository
    branch         = var.branch
    bucket         = local.bucket_name
    region         = var.region
    telegram_token = var.telegram_bot_token
    admin_password = var.admin_password
    code_url       = var.code_url
    log_url        = var.log_url
    backup_key     = ""
    backup_secret  = ""
  })

  root_block_device {
    volume_size = 16
    volume_type = "gp3"
  }

  tags = { Name = var.name, Project = var.name }
}

resource "aws_eip" "bot" {
  instance = aws_instance.bot.id
  tags     = { Name = "${var.name}-ip" }
}

output "ip" {
  value = aws_eip.bot.public_ip
}

output "shell" {
  description = "Browser shell: AWS console -> EC2 -> the instance -> Connect -> Session Manager"
  value       = "aws ssm start-session --target ${aws_instance.bot.id} --region ${var.region}"
}



# The bot only makes outbound connections to Telegram, so nothing needs to
# reach it. SSH is the single exception, and only for the person deploying.






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






# ---------------------------------------------------------------------------



output "backup_bucket" {
  value = aws_s3_bucket.backups.bucket
}




