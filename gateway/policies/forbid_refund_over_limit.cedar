// Refunds above $1,000 are forbidden outright.
//
// This looks redundant next to refund_limit.cedar: that permit only covers
// amounts <= 100000, and the engine denies by default, so an over-limit call is
// already refused today. The redundancy is the point.
//
// A permit-only design denies the over-limit case *by omission*. The day someone
// adds a broad `permit (principal, action, resource)` — while debugging, or to
// onboard a fourth tool in a hurry — the omission disappears and $5,000 refunds
// start succeeding, with no change to any file that mentions refunds.
//
// In Cedar an explicit forbid always beats any permit and cannot be overridden.
// So this statement, and not the absence of a permit, is what actually holds the
// ceiling. The two together mean the limit survives careless edits elsewhere.
//
// Amounts are in US cents; 100000 is $1,000.
forbid (
  principal,
  action == AgentCore::Action::"refunds___process_refund",
  resource == AgentCore::Gateway::"${GATEWAY_ARN}"
)
when { context.input.amount_cents > 100000 };
