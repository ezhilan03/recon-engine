variable "enable_publisher" {
  type    = bool
  default = false
}
variable "github_repository" {
  type    = string
  default = "ezhilan03/recon-engine"
}
resource "aws_iam_openid_connect_provider" "github" {
  count          = var.enable_publisher ? 1 : 0
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}
resource "aws_iam_role" "publisher" {
  count = var.enable_publisher ? 1 : 0
  name  = "${var.name}-publisher"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = aws_iam_openid_connect_provider.github[0].arn }
      Condition = { StringEquals = {
        "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        "token.actions.githubusercontent.com:sub" = "repo:${var.github_repository}:environment:recon-release"
      } }
    }]
  })
}
resource "aws_iam_role_policy" "publisher" {
  count = var.enable_publisher ? 1 : 0
  role  = aws_iam_role.publisher[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*" },
      { Effect = "Allow", Action = ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:DescribeImages"], Resource = aws_ecr_repository.batch.arn },
      { Effect = "Allow", Action = ["s3:PutObject", "s3:GetObject", "s3:GetObjectVersion"], Resource = ["${aws_s3_bucket.artifacts.arn}/inputs/*", "${aws_s3_bucket.artifacts.arn}/releases/*"] }
    ]
  })
}
output "publisher_role_arn" {
  value = var.enable_publisher ? aws_iam_role.publisher[0].arn : null
}
