mock_provider "aws" {}
variables { account_id = "123456789012" }
run "private_foundation" {
  command = plan
  assert {
    condition     = aws_s3_bucket_public_access_block.artifacts.block_public_acls && aws_s3_bucket_public_access_block.artifacts.block_public_policy && aws_s3_bucket_public_access_block.artifacts.ignore_public_acls && aws_s3_bucket_public_access_block.artifacts.restrict_public_buckets
    error_message = "The artifact bucket must block all public access."
  }
  assert {
    condition     = aws_ecr_repository.batch.image_tag_mutability == "IMMUTABLE" && !aws_ecr_repository.batch.force_delete
    error_message = "Release tags must be immutable and repository deletion must not discard images."
  }
  assert {
    condition     = !aws_s3_bucket.artifacts.force_destroy
    error_message = "Destroy must not discard stored data."
  }
}
run "bounded_demo" {
  # Mocked apply only: resolves provider-computed ARNs, creates no AWS resources.
  command = apply
  variables {
    enable_demo      = true
    enable_publisher = true
    demo_ami         = "ami-0123456789abcdef0"
  }
  assert {
    condition     = aws_instance.demo[0].instance_type == "t4g.small" && aws_instance.demo[0].credit_specification[0].cpu_credits == "standard"
    error_message = "Keep demo compute bounded and disable surplus credit billing."
  }
  assert {
    condition     = aws_instance.demo[0].metadata_options[0].http_tokens == "required" && length(aws_security_group.demo[0].ingress) == 0
    error_message = "Require IMDSv2 and no inbound ports."
  }
  assert {
    condition     = aws_instance.demo[0].root_block_device[0].encrypted && !aws_instance.demo[0].root_block_device[0].delete_on_termination
    error_message = "Database disk must be encrypted and retained."
  }
  assert {
    condition     = jsondecode(aws_iam_role.publisher[0].assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:ezhilan03/recon-engine:environment:recon-release"
    error_message = "Publisher must trust only the intended repository environment."
  }
}
