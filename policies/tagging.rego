# policies/tagging.rego
# Enforce mandatory tags on all resources

package terraform

deny[msg] {
    resource := input.resource_changes[_]
    actions := resource.change.actions
    actions[_] == "create"

    after := resource.change.after
    tags := object.get(after, "tags", {})
    not tags.Environment
    msg := sprintf("Resource '%s' must have an 'Environment' tag", [resource.address])
}

deny[msg] {
    resource := input.resource_changes[_]
    actions := resource.change.actions
    actions[_] == "create"

    after := resource.change.after
    tags := object.get(after, "tags", {})
    not tags.Owner
    msg := sprintf("Resource '%s' must have an 'Owner' tag", [resource.address])
}

deny[msg] {
    resource := input.resource_changes[_]
    actions := resource.change.actions
    actions[_] == "create"

    after := resource.change.after
    tags := object.get(after, "tags", {})
    not tags.ManagedBy
    msg := sprintf("Resource '%s' must have a 'ManagedBy' tag", [resource.address])
}
