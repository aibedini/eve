"""The ONE candidate gate ladder, shared by the real scan and the preview.

Why this module exists: the preview used to answer ``matched == eligible ==
len(candidates)`` with ``deferred = suppressed = 0``. That is not an evaluation -
it promised every matched candidate was eligible while the real run would defer
most of them, and it was the same shape of defect as ``matched_count ==
eligible_count`` in the run manifest.

A second implementation of the ladder would be WORSE than the stub, because the
two would drift and the preview would keep lying with more confidence. So the
order and the outcome of every per-candidate gate live here, once, and both
callers ask this module.

Pure by construction: no I/O, no database, no clock, no logging. The caller
resolves the facts (which needs the database); this decides (which must not).
That split is also what makes the preview safe to run against production data.
"""

# ── Per-candidate outcomes ────────────────────────────────────────────────────
ELIGIBLE_NOW = 'eligible_now'
DEFERRED = 'deferred'
SUPPRESSED = 'suppressed'
INVALID_RECIPIENT = 'invalid_recipient'
ACTIVE_OBLIGATION = 'active_obligation'

# Every outcome a caller may see, for validation and for the preview summary.
DISPOSITIONS = (ELIGIBLE_NOW, DEFERRED, SUPPRESSED, INVALID_RECIPIENT,
                ACTIVE_OBLIGATION)

# ── Run-level preconditions ───────────────────────────────────────────────────
RUN_READY = 'ready'
RUN_SMS_DISABLED = 'sms_disabled'
RUN_GATEWAY_NOT_READY = 'gateway_not_ready'
RUN_QUIET_HOURS = 'quiet_hours'

#: Run-level states that make every candidate non-eligible.
RUN_BLOCKS = {
    RUN_SMS_DISABLED: 'sms_disabled',
    RUN_GATEWAY_NOT_READY: 'gateway_not_ready',
    RUN_QUIET_HOURS: 'quiet_hours',
}


class CandidateFacts:
    """The resolved facts about ONE candidate. Every field is a plain value.

    Nothing here performs a lookup: a caller that cannot resolve a fact must pass
    the honest default rather than a guess, because a guessed fact becomes a
    wrong decision that is indistinguishable from a measured one.
    """

    __slots__ = ('has_recipient', 'opted_out', 'manual_review',
                 'obligation_outstanding', 'cooldown_seconds_remaining',
                 'template_present', 'message_empty', 'budget_available',
                 'budget_stop_reason')

    def __init__(self, *, has_recipient=True, opted_out=False, manual_review=False,
                 obligation_outstanding=False, cooldown_seconds_remaining=0,
                 template_present=True, message_empty=False, budget_available=True,
                 budget_stop_reason=None):
        self.has_recipient = bool(has_recipient)
        self.opted_out = bool(opted_out)
        self.manual_review = bool(manual_review)
        self.obligation_outstanding = bool(obligation_outstanding)
        self.cooldown_seconds_remaining = int(cooldown_seconds_remaining or 0)
        self.template_present = bool(template_present)
        self.message_empty = bool(message_empty)
        self.budget_available = bool(budget_available)
        self.budget_stop_reason = budget_stop_reason


class Evaluation:
    """What the ladder decided about one candidate."""

    __slots__ = ('disposition', 'reason_code', 'sendable', 'stop_reason')

    def __init__(self, disposition, reason_code, sendable, stop_reason=None):
        self.disposition = disposition
        self.reason_code = reason_code
        self.sendable = bool(sendable)
        # Set only when the candidate cannot be sent AND the whole run should
        # stop: an exhausted daily/hourly budget is not one candidate's problem.
        self.stop_reason = stop_reason

    def __eq__(self, other):
        return (isinstance(other, Evaluation)
                and (self.disposition, self.reason_code, self.sendable,
                     self.stop_reason)
                == (other.disposition, other.reason_code, other.sendable,
                    other.stop_reason))

    def __repr__(self):
        return ('Evaluation(disposition=%r, reason_code=%r, sendable=%r, stop_reason=%r)'
                % (self.disposition, self.reason_code, self.sendable, self.stop_reason))


def evaluate_run(*, sms_enabled, gateway_ready, quiet_hours):
    """The run-level preconditions, in the order the real scan applies them.

    Returns RUN_READY or one of RUN_BLOCKS. Kept separate from the per-candidate
    ladder because it decides whether the run may act at all, not what to do with
    a particular candidate - and the preview must report it rather than pretend
    every candidate is eligible during quiet hours.
    """
    if not sms_enabled:
        return RUN_SMS_DISABLED
    if not gateway_ready:
        return RUN_GATEWAY_NOT_READY
    if quiet_hours:
        return RUN_QUIET_HOURS
    return RUN_READY


#: Budget refusals that stop the WHOLE RUN rather than one candidate. Exhausting
#: the daily or hourly budget is not one candidate's problem, so the distinction
#: belongs here rather than at the call site that happens to see the reason.
RUN_STOPPING_BUDGET_REASONS = ('daily_limit_reached', 'hourly_limit_reached')


def evaluate_candidate(facts):
    """Decide ONE candidate. The order below IS the policy.

    Order matters and is deliberate:
      hard suppressions (no address, opted out, under manual review) first,
      then an already-outstanding obligation (do not tell the same customer the
      same thing twice), then soft deferrals (cooldown, budget), then the content
      gates, then eligible.
    """
    if not facts.has_recipient:
        return Evaluation(INVALID_RECIPIENT, 'no_recipient', False)
    if facts.opted_out:
        return Evaluation(SUPPRESSED, 'opted_out_recheck', False)
    if facts.manual_review:
        return Evaluation(SUPPRESSED, 'manual_review_pending', False)
    if facts.obligation_outstanding:
        return Evaluation(ACTIVE_OBLIGATION, 'obligation_outstanding', False)
    if facts.cooldown_seconds_remaining > 0:
        return Evaluation(DEFERRED, 'cooldown_active', False)
    if not facts.template_present:
        return Evaluation(SUPPRESSED, 'no_template', False)
    if facts.message_empty:
        return Evaluation(SUPPRESSED, 'empty_message', False)
    if not facts.budget_available:
        # A budget stop is a deferral with a reason, and only an exhausted
        # daily/hourly budget stops the run; a per-send refusal does not.
        reason = facts.budget_stop_reason or 'rate_limited'
        return Evaluation(DEFERRED, reason, False,
                          stop_reason=(reason if reason in RUN_STOPPING_BUDGET_REASONS
                                       else None))
    return Evaluation(ELIGIBLE_NOW, None, True)


def summarize(evaluations, *, run_state=RUN_READY):
    """Aggregate per-candidate evaluations into the preview's numbers.

    ``matched`` is the number of candidates the cheap scan found. A run-level
    block makes every one of them non-eligible, and says so with the run's reason
    rather than silently counting them as deferred candidates.
    """
    summary = {'matched': 0, ELIGIBLE_NOW: 0, DEFERRED: 0, SUPPRESSED: 0,
               INVALID_RECIPIENT: 0, ACTIVE_OBLIGATION: 0, 'reasons': {}}
    for evaluation in evaluations:
        summary['matched'] += 1
        if run_state != RUN_READY:
            summary[DEFERRED] += 1
            reason = RUN_BLOCKS.get(run_state, run_state)
        else:
            summary[evaluation.disposition] += 1
            reason = evaluation.reason_code
        if reason:
            summary['reasons'][reason] = summary['reasons'].get(reason, 0) + 1
    summary['run_state'] = run_state
    summary['eligible'] = summary[ELIGIBLE_NOW]
    return summary
