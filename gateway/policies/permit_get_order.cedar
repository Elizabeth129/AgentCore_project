// Reading an order is always allowed for an authenticated caller.
//
// The engine denies by default — a tool call with no matching permit is refused
// with "No policy applies to the request (denied by default)". So every tool the
// agent is supposed to use needs a permit here, or it stops working.
//
// `${GATEWAY_ARN}` is substituted by `scripts/build_policies.py` before deploy.
// The service leaves no choice about naming the gateway: an unconstrained
// `resource` is rejected ("a wildcard resource was detected"), and so is the
// resource *type* `resource is AgentCore::Gateway` ("please constrain the
// resource to a specific AgentCore::Gateway resource when creating tool-specific
// policies"). Since the ARN contains the account id, which must not be committed
// (CLAUDE.md §6), these files are templates and the ARN is rendered at build time
// from the deployed stack's output.
permit (
  principal,
  action == AgentCore::Action::"orders___get_order",
  resource == AgentCore::Gateway::"${GATEWAY_ARN}"
);
