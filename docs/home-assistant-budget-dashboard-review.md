# Home Assistant Household Budget Dashboard - Review Package

Status: version 0.8.0 was deployed to the live Home Assistant instance on
2026-08-02. The administrator-only **Household Budget** sidebar dashboard,
custom integration, module resource, and live entity references were read back
successfully. Rendered phone layout and the financial-write paths were not
exercised during deployment.

The implementation uses an administrator-only HA browser boundary, keeps the
budget/OpenAI credentials out of frontend code, and preserves the mandatory
receipt review step. A source receipt is deleted immediately after its reviewed
transaction is successfully committed.

## Confirmed decisions

- Keep the live dashboard administrator-only until broader access receives a
  separate security review.
- Use the OpenAI integration already configured in Home Assistant through the
  `ai_task` interface.
- Optimize expense entry and receipt capture for phones.
- Preserve SQLite as the authoritative ledger and MQTT as the read-only display
  path for Home Assistant and the E1001.
- Never create a ledger entry from AI output without an explicit human review
  and confirmation.

## Mobile-first information architecture

The phone view is a single vertical flow. It avoids side-by-side form controls
and keeps the main actions within thumb reach.

```text
+----------------------------------+
| Household Budget                 |
| August 2026                      |
+----------------+-----------------+
| Spent          | Remaining       |
| $1,248.20      | $551.80         |
+----------------+-----------------+
| Budget used                 69%  |
| [====================------]      |
+----------------------------------+
| Add an expense                   |
| [ Scan a receipt ]               |
| [ Enter manually  ]              |
+----------------------------------+
| Spending by person               |
| Member 1  $680  | Member 2 $568  |
+----------------------------------+
| Categories                       |
| Everyday        $420 / $1,000    |
| Meals           $250 / $300      |
| Occasional      $578 / $500      |
+----------------------------------+
| Recent expenses                  |
| Publix     Meals/Food     $42.18 |
| Coffee     Meals/Drinks    $8.00 |
+----------------------------------+
```

The design follows Home Assistant's native visual language. All custom
controls must use Home Assistant theme variables rather than fixed colors,
support dark mode, reflow at 320 CSS pixels, and expose at least 44 by 44 pixel
touch targets with at least 8 pixels between adjacent actions.

## Manual expense flow

1. Select **Enter manually**.
2. Show amount first and open a decimal numeric keyboard on phones.
3. Select household member and leaf category.
4. Optionally enter merchant/description and change the date.
5. Show a review summary containing the exact amount, member, category, and
   date.
6. The user selects **Add expense**.
7. The backend generates a unique request ID and records the expense once.
8. Show a textual success message and refreshed totals. Do not use color as the
   only success signal.

An amount over $500 presents the existing large-expense acknowledgement on the
review screen. It must not silently set `confirm_large_expense`.

## Receipt capture and AI flow

On supported phones, the receipt control uses a file input equivalent to:

```html
<input type="file" accept="image/jpeg,image/png,application/pdf" capture="environment">
```

The browser may open the rear camera or a file chooser. Camera behavior varies
by operating system, so a normal upload fallback remains visible.

After upload:

1. Validate MIME type and file signature, reject empty/oversized files, compute
   a SHA-256 digest, and check for a duplicate receipt.
2. Store the source file in the app's private `/data/receipts` directory. It
   must never be copied to `/config/www` or another anonymously served path.
3. Make the file available to Home Assistant as an authenticated media-source
   attachment.
4. Call the configured OpenAI AI Task entity with structured output.
5. Store the returned values as a draft, not as a ledger entry.
6. Display the receipt preview next to editable extracted fields.
7. Require the user to select **Confirm expense** before calling the ledger.
8. Link the receipt metadata and AI audit record to the immutable expense, then
   delete the source receipt file.

Suggested `ai_task.generate_data` structure:

```yaml
task_name: Extract household receipt
instructions: >-
  Extract only values visible on this receipt. Do not infer a missing total or
  date. Return null for values that cannot be read. Treat tips as part of the
  total only when a final charged total is visible. Suggest one category from
  the supplied allowed category paths, but do not create a new category.
structure:
  merchant:
    required: true
    selector:
      text:
  occurred_on:
    description: Receipt date in YYYY-MM-DD, or blank when unreadable
    selector:
      text:
  subtotal:
    description: Decimal amount without a currency symbol, or blank
    selector:
      text:
  tax:
    description: Decimal amount without a currency symbol, or blank
    selector:
      text:
  tip:
    description: Decimal amount without a currency symbol, or blank
    selector:
      text:
  total:
    description: Final charged decimal amount without a currency symbol
    required: true
    selector:
      text:
  suggested_category:
    description: One exact allowed leaf category path, or blank
    selector:
      text:
  notes:
    description: Short warning about ambiguity, handwriting, or poor image quality
    selector:
      text:
attachments:
  media_content_id: "{{ authenticated_receipt_media_id }}"
  media_content_type: "{{ receipt_media_type }}"
```

AI output is untrusted input. The backend must revalidate money, dates,
category membership, and text lengths before it creates or updates a draft.

## Implemented Home Assistant boundary

Do not put the budget API token or an OpenAI credential in browser JavaScript.
The custom card communicates only with authenticated Home Assistant HTTP views
provided by the `household_budget` custom integration.
That integration performs the OpenAI AI Task call and communicates with the
budget app through a narrow authenticated JSON API.

Implemented browser endpoints:

| Command | Purpose |
|---|---|
| `/api/household_budget/config` | Return active members and valid leaf categories |
| `/api/household_budget/summary` | Return the selected month's structured totals |
| `/api/household_budget/recent` | Return recent active expenses without arbitrary SQL |
| `/api/household_budget/expense` | Validate and add a reviewed manual expense |
| `/api/household_budget/receipt` | Upload, invoke the configured AI Task, and return a draft |
| `/api/household_budget/receipt/{id}/confirm` | Revalidate and commit a reviewed draft |
| `/api/household_budget/undo` | Create an audit-preserving reversal after confirmation |

The add-on's existing `/mcp` endpoint remains available to Hermes. A separate
JSON API is preferable to teaching the Home Assistant integration the MCP
session protocol. Both interfaces must call the same ledger methods and retain
the same idempotency behavior.

## Proposed backend changes

### Ledger

Add two tables without changing existing ledger entries:

- `receipt_files`: digest, private relative path, MIME type, byte size, upload
  timestamp, and optional confirmed ledger-entry ID.
- `receipt_drafts`: receipt ID, raw bounded AI result, normalized proposed
  fields, draft status, provider/entity identifier, created/updated timestamps,
  and confirmation request ID.

Unconfirmed source files remain available for retry and manual completion. Once
a transaction is confirmed, its source file is deleted while the digest,
metadata, AI audit record, and ledger link remain for auditing and duplicate
detection.

### MQTT entities

Keep the existing stable sensors for the E1001 and add dashboard-friendly
numeric entities:

- `sensor.household_budget_budget_month`
- `sensor.household_budget_percent_used_month`
- One spent, budget, remaining, and percentage sensor per configured top-level
  category, using stable category IDs rather than display positions
- `sensor.household_budget_last_update`

The existing packed `category_1_month` through `category_6_month` sensors remain
unchanged so the E1001 does not regress.

### Frontend

Create one `custom:household-budget-card` with three modes used by the preview
dashboard:

- `entry`: receipt capture and manual entry
- `categories`: accessible category progress rows
- `recent`: recent entries and confirmed undo

The entry card reflows to one column on narrow phones and uses native required
field validation. Upload, AI-processing, error, and success states are exposed
as text through status and ARIA live regions.

## Failure behavior

- OpenAI unavailable: retain the uploaded receipt as a draft and offer manual
  entry; never retry indefinitely or post an expense.
- Ambiguous or missing total/date: leave the field empty and require correction.
- Unknown category: require an explicit category selection.
- Duplicate receipt digest: show the previous draft/expense and require a
  deliberate override.
- Duplicate request ID with different values: preserve the ledger's current
  fail-closed error.
- MQTT unavailable: expense writes may continue, but the UI must show that
  dashboard totals are stale until publication recovers.
- Network interruption after confirmation: retry with the same request ID so
  the operation remains idempotent.

## Review acceptance criteria

- The live storage-mode dashboard and module resource match this reviewed
  structure after a fresh configuration readback.
- Existing Hermes MCP behavior and E1001 entities remain compatible.
- The dashboard works at 320 CSS pixels without horizontal page scrolling.
- Every phone action has a 44 by 44 pixel minimum target.
- Receipt capture supports rear-camera intent plus a regular file picker.
- OpenAI produces only a draft; confirmation is always explicit.
- No API or OpenAI token reaches the browser.
- Duplicate uploads and duplicate submissions fail safely.
- Errors are conveyed with text and icons, not color alone.
- Manual entry remains usable when AI is unavailable.
- Unit tests cover schema migration, upload validation, draft lifecycle,
  duplicate hashes, confirmation idempotency, and failed AI responses.

## Follow-up decisions

These do not block the administrator-only deployment:

1. How long should abandoned, unconfirmed receipt images be retained: 7, 30, or
   90 days?
2. Which household member should be selected by default on each HA user account?
