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
