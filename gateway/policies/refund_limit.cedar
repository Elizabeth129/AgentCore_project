// A refund is allowed only up to the $1,000 ceiling.
//
// `context.input` holds the tool call's arguments as the model produced them, so
// the amount evaluated here is the amount that would actually be refunded — not
// something the model asserts separately. The check happens at the Gateway,
// before the Lambda is invoked at all.
//
// 100000 is US cents. The tool schema declares amount_cents as an integer, so
// there is no unit ambiguity and no float comparison.
permit (
  principal,
  action == AgentCore::Action::"refunds___process_refund",
  resource == AgentCore::Gateway::"${GATEWAY_ARN}"
)
when { context.input.amount_cents <= 100000 };
