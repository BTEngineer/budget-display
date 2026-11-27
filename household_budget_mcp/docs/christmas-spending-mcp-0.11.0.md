# Christmas spending MCP 0.11.0

Version 0.11.0 adds seven tools to the original fourteen-tool MCP surface.
Christmas records are audited attribution over the existing ledger, not another
financial ledger. Existing expense, receipt, reconciliation, category, and
monthly-budget interfaces retain their contracts.

## Explicit requests only

Use Christmas attribution only when the person specifically requests it for a
purchase or identified group of purchases. Receipt uploads, AI extraction, item
descriptions, merchants, purchase dates, and inferred categories provide no such
request and must not trigger attribution or unsolicited Christmas questions.

`set_christmas_attribution` and `set_christmas_refund_attribution` require
`explicitly_requested: true`. The boolean is strict: false, missing, numeric,
and string values are rejected. It records the caller's assertion of an explicit
request; callers must still follow the person's instruction. An instruction such
as "record these as Christmas gifts" already supplies opt-in and needs no second
opt-in question. Existing financial confirmation rules still apply when posting
the expense itself.

Ordinary receipt analysis and confirmation never call a Christmas write tool.
Linked refunds can inherit an original purchase's existing explicit attribution;
this is not detection of Christmas intent in a new receipt.

## Tools

| Tool | Purpose |
| --- | --- |
| `upsert_christmas_recipient` | Create, rename, activate, or deactivate a reusable recipient. |
| `list_christmas_recipients` | List recipients, optionally including inactive names. |
| `configure_christmas_year` | Set or clear one year's annual limit and set its child comparison group. |
| `set_christmas_attribution` | Assign or replace a purchase's Christmas gifts, recipient shares, overhead, and ordinary remainder. |
| `set_christmas_refund_attribution` | Resolve a linked refund's specific gift, overhead, and ordinary amounts. |
| `get_christmas_summary` | Calculate complete annual totals, recorded child values, gaps, and completeness warnings on the server. |
| `list_christmas_attributions` | Read bounded purchase details and attribution/configuration/refund audit history. |

All writes require a stable `request_id`. Use distinct IDs for posting the
expense and assigning its Christmas details. An exact retry returns the stored
result with `duplicate: true`; conflicting reuse is rejected. Retrying an older
request never reapplies it over a newer revision.

## Recipients and annual setup

`upsert_christmas_recipient(request_id, name, recipient_id?, active=true)` returns
a stable `recipient_id`. Omit the ID for creation; supply it to rename an
existing recipient. Names are unique ignoring SQLite's case-insensitive name
comparison. The registry is bounded to 100 recipients. Recipients are separate
from the household member who paid for an expense.

`list_christmas_recipients(include_inactive=false)` returns `recipients`.
Deactivation preserves historical attribution.

`configure_christmas_year(request_id, year, child_recipient_ids?, budget_amount?,
clear_budget=false)` changes only supplied settings for the named year. Amounts
are decimal strings with at most two places. The limit may be zero; an absent
limit is represented by null, not zero. `clear_budget=true` removes an existing
limit and cannot accompany `budget_amount`.

Each year retains its own comparison group and limit history. Nothing rolls
over automatically. A limit or comparison group need not exist before recording
a purchase. Limits and equality are information and never block actual spending.

## Purchase attribution

First use the normal expense or split-expense workflow. Then call:

```json
{
  "request_id": "message-101-christmas",
  "transaction_id": "<posted transaction ID or split purchase ID>",
  "explicitly_requested": true,
  "year": 2026,
  "gifts": [
    {
      "gift_id": "shared-game",
      "description": "Shared game",
      "amount": "100.00",
      "recipient_ids": ["<first recipient ID>", "<second recipient ID>"]
    }
  ],
  "overhead_amount": "10.00",
  "ordinary_amount": "40.00"
}
```

For a $150 purchase, this records $110 of Christmas spending and $50 gift value
per child, while monthly spending remains $150. It creates no financial expense.
The annual total includes Christmas tax, shipping, wrapping, decorations, and
other seasonal costs; child values include only discounted gift-item prices net
of returns.

The purchase can be a normal expense, a complete split group, or any expense
allocation belonging to that group. These aliases resolve to the same whole
purchase so they cannot count it twice. Corrections resolve through the original
purchase's stable identity.

Arguments are `request_id`, `transaction_id`, `gifts`, `explicitly_requested`,
optional `overhead_amount="0.00"`, `ordinary_amount=null`, and `year=null`.
On first attribution an omitted year defaults to the purchase's household-local
year; on edits it preserves the existing intended Christmas year. Explicitly
supplying a year moves the attribution and linked returns to that Christmas.

Each of at most 100 gifts contains a description and positive decimal amount.
`gift_id` is optional on creation; retain the returned stable gift IDs when
editing, especially after refunds. Use either `recipient_ids` for equal shares
or `recipient_amounts=[{"recipient_id":"...","amount":"..."}]` for explicit
shares that sum exactly to the gift amount. Omitted/empty recipients produce a
visibly unassigned gift, included in annual spending with a comparison warning.

Gift amounts, overhead, and ordinary portions must reconcile exactly to the
purchase. If `ordinary_amount` is omitted, the remainder is ordinary spending;
it is never assumed to be more Christmas spending. Empty gifts support
overhead-only attribution. To remove a mistaken Christmas assignment, explicitly
replace it with empty gifts, zero overhead, and the whole purchase as ordinary;
its previous assignment remains in history. That explicit clearing atomically
makes existing linked refund breakdowns ordinary as well, so it also works after
partial or full returns. Later linked returns remain ordinary.

## Returns and financial corrections

Record refunds with the existing `refund_expense` tool. A full linked refund
reduces its original Christmas allocation to zero, including cumulative partial
refunds that together return the whole purchase. A partial refund is inherited
when only one remaining component can receive it. When gift-versus-overhead or
ordinary portions are ambiguous, the report exposes unresolved amounts and asks
for a breakdown instead of guessing.

Resolve an ambiguous refund with `set_christmas_refund_attribution`:

```json
{
  "request_id": "message-102-christmas-refund",
  "transaction_id": "<linked refund transaction ID>",
  "explicitly_requested": true,
  "gifts": [{"gift_id": "shared-game", "amount": "20.00"}],
  "overhead_amount": "1.00",
  "ordinary_amount": "0.00"
}
```

These portions must equal the refund total and cannot exceed the original
gift/overhead/ordinary balances. This example reduces annual spending by $21
and shared gift value by $20. Shared recipients keep proportional shares with
deterministic cent rounding; further refunds cannot increase a recipient's value.

A January return remains January financial cash flow while reducing the
original Christmas year. Refund resolution requires a verified linked purchase
that already has explicit Christmas attribution. An unlinked financial refund
is retained by the ordinary ledger and is not guessed into Christmas.

Same-total financial corrections carry attribution through the correction
chain. A changed purchase total produces an incomplete-attribution warning until
its breakdown is explicitly updated. Reversing a purchase removes its effective
Christmas spending while preserving all financial and attribution history.

## Reports and audit history

`get_christmas_summary(year)` returns cent values in USD, including
`budget_cents`, `spent_cents`, `remaining_cents`, `overspent_cents`,
`child_totals`, `by_recipient_cents`, `unassigned_gift_cents`,
`unresolved_refund_cents`, `comparison_complete`, and `warnings`.

Show warnings alongside amounts. Each selected child appears even with zero
recorded gift value; that zero does not prove the child received no gifts.
Complete child comparisons include `gift_value_cents` and
`gap_to_highest_cents`. Incomplete exact values are null, with explicitly labelled
known amounts available for explanation. If a refund's gift/overhead split is
unknown but the entire purchase was Christmas, the annual net remains knowable;
child comparisons still remain incomplete. Mixed ordinary/Christmas ambiguity
also leaves annual net and remaining amounts unresolved. An unset limit always
leaves remaining/overspent values null.

`list_christmas_attributions(year, limit=50, offset=0)` limits each page to at most
200 attributions and 200 history events. Its `offset`, `has_more`, and
`history_has_more` fields describe the page; increase offset by limit until both
flags are false. Totals must come from `get_christmas_summary`, never by adding
one detail page. Read operations use a single SQLite snapshot.

## Storage, compatibility, and release scope

Five additive `christmas_*` tables store recipients, yearly settings, current
attribution, refund breakdowns, and immutable request/result history. Startup
validates their schema and marker; a missing or inconsistent previously used
schema fails closed. No original ledger table is rebuilt or backfilled with
Christmas assumptions. Prior code ignores the additive tables.

The [Hermes example](../hermes-config.example.yaml) includes all twenty-one tools,
with resources, prompts, and parallel calls disabled. Deploying a new server and
changing a running client's allowlist are separate operational steps. Enable
the new client tools only with the matching deployed server.
