# policies/s3_encryption.rego
# Deny S3 buckets missing encryption or public access blocks

package terraform

deny[msg] {
    resource := input.resource_changes[_]
    resource.type == "aws_s3_bucket"
    actions := resource.change.actions
    actions[_] == "create"

    # Check if a corresponding encryption config exists
    not has_encryption(resource.change.after.bucket)
    msg := sprintf("S3 bucket '%s' must have server-side encryption configured", [resource.address])
}

deny[msg] {
    resource := input.resource_changes[_]
    resource.type == "aws_s3_bucket"
    actions := resource.change.actions
    actions[_] == "create"

    not has_public_access_block(resource.change.after.bucket)
    msg := sprintf("S3 bucket '%s' must have public access block configured", [resource.address])
}

deny[msg] {
    resource := input.resource_changes[_]
    resource.type == "aws_s3_bucket_public_access_block"
    after := resource.change.after

    not after.block_public_acls
    msg := sprintf("Public access block '%s' must set block_public_acls = true", [resource.address])
}

deny[msg] {
    resource := input.resource_changes[_]
    resource.type == "aws_s3_bucket_public_access_block"
    after := resource.change.after

    not after.block_public_policy
    msg := sprintf("Public access block '%s' must set block_public_policy = true", [resource.address])
}

deny[msg] {
    resource := input.resource_changes[_]
    resource.type == "aws_s3_bucket_public_access_block"
    after := resource.change.after

    not after.restrict_public_buckets
    msg := sprintf("Public access block '%s' must set restrict_public_buckets = true", [resource.address])
}

has_encryption(bucket_name) {
    resource := input.resource_changes[_]
    resource.type == "aws_s3_bucket_server_side_encryption_configuration"
    resource.change.actions[_] != "delete"
}

has_public_access_block(bucket_name) {
    resource := input.resource_changes[_]
    resource.type == "aws_s3_bucket_public_access_block"
    resource.change.actions[_] != "delete"
}
