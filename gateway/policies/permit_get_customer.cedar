// Reading a customer record is always allowed for an authenticated caller.
// See permit_get_order.cedar for why the gateway ARN is a `${GATEWAY_ARN}`
// placeholder rather than a literal.
permit (
  principal,
  action == AgentCore::Action::"customers___get_customer",
  resource == AgentCore::Gateway::"${GATEWAY_ARN}"
);
