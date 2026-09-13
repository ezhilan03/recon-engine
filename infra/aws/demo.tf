variable "enable_demo" {
  type    = bool
  default = false
}
variable "demo_ami" {
  type        = string
  default     = null
  description = "Pinned Amazon Linux 2023 ARM64 AMI, resolved and reviewed before apply."
}
resource "aws_vpc" "demo" {
  count                = var.enable_demo ? 1 : 0
  cidr_block           = "10.73.0.0/24"
  enable_dns_support   = true
  enable_dns_hostnames = true
}
resource "aws_subnet" "demo" {
  count                   = var.enable_demo ? 1 : 0
  vpc_id                  = aws_vpc.demo[0].id
  cidr_block              = "10.73.0.0/26"
  map_public_ip_on_launch = true
}
resource "aws_internet_gateway" "demo" {
  count  = var.enable_demo ? 1 : 0
  vpc_id = aws_vpc.demo[0].id
}
resource "aws_route_table" "demo" {
  count  = var.enable_demo ? 1 : 0
  vpc_id = aws_vpc.demo[0].id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.demo[0].id
  }
}
resource "aws_route_table_association" "demo" {
  count          = var.enable_demo ? 1 : 0
  subnet_id      = aws_subnet.demo[0].id
  route_table_id = aws_route_table.demo[0].id
}
resource "aws_security_group" "demo" {
  count       = var.enable_demo ? 1 : 0
  name_prefix = "${var.name}-"
  vpc_id      = aws_vpc.demo[0].id
  ingress     = []
  # No inbound ports. Operations use the SSM agent's outbound connection.
  egress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
resource "aws_iam_role" "demo" {
  count = var.enable_demo ? 1 : 0
  name  = "${var.name}-demo"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "ec2.amazonaws.com" } }]
  })
}
resource "aws_iam_role_policy_attachment" "ssm" {
  count      = var.enable_demo ? 1 : 0
  role       = aws_iam_role.demo[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}
resource "aws_iam_role_policy" "demo" {
  count = var.enable_demo ? 1 : 0
  role  = aws_iam_role.demo[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["ecr:GetAuthorizationToken", "logs:DescribeLogGroups"], Resource = "*" },
      { Effect = "Allow", Action = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"], Resource = aws_ecr_repository.batch.arn },
      { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = ["${aws_s3_bucket.artifacts.arn}/inputs/*", "${aws_s3_bucket.artifacts.arn}/releases/*"] },
      { Effect = "Allow", Action = ["s3:PutObject"], Resource = ["${aws_s3_bucket.artifacts.arn}/reports/*", "${aws_s3_bucket.artifacts.arn}/backups/*"] },
      { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"], Resource = "${aws_cloudwatch_log_group.batch.arn}:*" }
    ]
  })
}
resource "aws_iam_instance_profile" "demo" {
  count = var.enable_demo ? 1 : 0
  role  = aws_iam_role.demo[0].name
}
resource "aws_instance" "demo" {
  count                                = var.enable_demo ? 1 : 0
  ami                                  = var.demo_ami
  instance_type                        = "t4g.small"
  subnet_id                            = aws_subnet.demo[0].id
  vpc_security_group_ids               = [aws_security_group.demo[0].id]
  iam_instance_profile                 = aws_iam_instance_profile.demo[0].name
  instance_initiated_shutdown_behavior = "stop"
  user_data_replace_on_change          = true
  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }
  credit_specification { cpu_credits = "standard" }
  root_block_device {
    volume_type           = "gp3"
    volume_size           = 16
    encrypted             = true
    delete_on_termination = false
  }
  lifecycle {
    precondition {
      condition     = var.demo_ami != null
      error_message = "Resolve and pin an AL2023 ARM64 AMI before planning the demo."
    }
  }
  user_data  = <<-SCRIPT
    #!/bin/bash
    set -euo pipefail
    # Install the shutdown bound BEFORE package setup so a failed bootstrap stops.
    cat >/etc/systemd/system/recon-stop.service <<'UNIT'
    [Unit]
    Description=Stop the bounded Recon demo session
    [Service]
    Type=oneshot
    ExecStart=/usr/sbin/shutdown -h now
    UNIT
    cat >/etc/systemd/system/recon-stop.timer <<'UNIT'
    [Unit]
    Description=Stop Recon after one hour of uptime
    [Timer]
    OnBootSec=60min
    Unit=recon-stop.service
    [Install]
    WantedBy=timers.target
    UNIT
    systemctl daemon-reload
    systemctl enable --now recon-stop.timer
    dnf install -y docker
    systemctl enable --now docker
    install -d -m 700 /var/lib/recon
    echo '${base64encode(file("${path.module}/../../scripts/demo_run.py"))}' | base64 -d >/usr/local/bin/recon-demo
    chmod 700 /usr/local/bin/recon-demo
    cat >/var/lib/recon/config.json <<'CONFIG'
    ${jsonencode({ bucket = aws_s3_bucket.artifacts.id, region = var.region, repository = aws_ecr_repository.batch.repository_url })}
    CONFIG
    chmod 600 /var/lib/recon/config.json
  SCRIPT
  tags       = { Name = "${var.name}-demo" }
  depends_on = [aws_route_table_association.demo, aws_iam_role_policy.demo, aws_iam_role_policy_attachment.ssm, aws_budgets_budget.demo]
}
output "demo_instance_id" {
  value = var.enable_demo ? aws_instance.demo[0].id : null
}
resource "aws_ssm_document" "demo" {
  count         = var.enable_demo ? 1 : 0
  name          = "${var.name}-run"
  document_type = "Command"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Run one synthetic Recon revision"
    parameters    = { Revision = { type = "String", allowedPattern = "^[0-9a-f]{40}$" } }
    mainSteps = [{
      action = "aws:runShellScript", name = "recon"
      inputs = { timeoutSeconds = "900", runCommand = ["set -eu", "cloud-init status --wait", "test -x /usr/local/bin/recon-demo", "/usr/local/bin/recon-demo {{ Revision }}"] }
    }]
  })
}
resource "aws_iam_role" "demo_operator" {
  count = var.enable_demo && var.enable_publisher ? 1 : 0
  name  = "${var.name}-operator"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = aws_iam_openid_connect_provider.github[0].arn }
      Condition = { StringEquals = {
        "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        "token.actions.githubusercontent.com:sub" = "repo:${var.github_repository}:environment:recon-demo"
      } }
    }]
  })
}
resource "aws_iam_role_policy" "demo_operator" {
  count = var.enable_demo && var.enable_publisher ? 1 : 0
  role  = aws_iam_role.demo_operator[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["ec2:StartInstances", "ec2:StopInstances"], Resource = aws_instance.demo[0].arn },
      { Effect = "Allow", Action = ["ec2:DescribeInstances", "ssm:DescribeInstanceInformation", "ssm:GetCommandInvocation"], Resource = "*" },
      { Effect = "Allow", Action = ["ssm:SendCommand"], Resource = [aws_instance.demo[0].arn, aws_ssm_document.demo[0].arn] }
    ]
  })
}
output "demo_operator_role_arn" {
  value = var.enable_demo && var.enable_publisher ? aws_iam_role.demo_operator[0].arn : null
}
output "demo_document" {
  value = var.enable_demo ? aws_ssm_document.demo[0].name : null
}
