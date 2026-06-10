# Recommended Firefly III Organization

A practical guide for keeping your Firefly III data **truthful** and **well-presented**, tailored to this setup:

- Income from a **monthly salary** and from **your own business**.
- **Pass-through money** (e.g. paying upfront for friends) that is neither real income nor real spending.
- **Monthly contracts**: insurances, mortgage, personal taxes.
- A **business bank account** that lives on a separate banking system and is **not** imported.
- An **automatic importer/bot** that creates transactions on your personal account.

Menu paths below refer to the Firefly III web UI. Note Firefly renamed **"Bills"** to **"Subscriptions"**; both names mean the same feature.

---

## 0. The one rule that explains everything

Firefly III is **double-entry**. The *type* of a transaction is decided entirely by what sits on each end, and that type decides whether it counts as income, spending, or neither:

| Type | Flow | Counts as | Budgets? |
|------|------|-----------|----------|
| **Deposit** | revenue account → your asset account | **Income** | No |
| **Withdrawal** | your asset account → expense account | **Spending** | Yes (only type that can) |
| **Transfer** | your account → your own account | **Neither** (invisible to income/expense) | No |

Consequences you must internalize:

- **Categories, tags, and transaction links only annotate.** They never add to or subtract from your headline income/expense totals.
- **There is no "exclude from reports" toggle** in Firefly (repeatedly requested, never built).
- Therefore, the *only* way to make money "not count" is to make it a **transfer** instead of a deposit/withdrawal.

Keep this table in mind for every decision below.

---

## 1. Two income streams: salary + business

Both salary and business money arrive on your personal account as **deposits**. Model each payer as its own **revenue account** so reports can answer "salary vs. business" directly.

### Set it up

1. You don't pre-create revenue accounts; they are created the first time you type a name into the **source** field of a deposit. To do it explicitly: **Accounts → Revenue accounts → Create new revenue account**.
2. Create / use:
   - Revenue account **"Employer"** — for the salary deposit.
   - Revenue account **"<Business> (owner's draw)"** — for money the business pays you.
3. When entering or correcting a salary deposit (**Transactions → Deposits → Create**, or by editing the imported line):
   - **Source** = `Employer`
   - **Destination** = your personal asset account
   - **Category** (optional) = `Salary`
4. For business money landing on your personal account, same thing with source = `<Business> (owner's draw)`, category `Business income`.

### Why revenue accounts (not categories/tags) for the primary split

- The dashboard "earned" total and **Reports → Expense/Revenue report** are organized around revenue accounts, so per-payer revenue accounts make income attribution truthful.
- **Categories** are a useful *secondary* layer: the **Category report can include income**, so a category lets you trend each stream over time.
- **Tags** are the loosest grouping — fine for ad-hoc spanning (a project, a trip, a tax year), not for the core income split.
- **Budgets do not apply to income** — Firefly cannot budget deposits.

### Split salary (optional)

If your payslip has components (base + bonus + tax), enter the salary as a **split deposit** (the "+" to add splits on the deposit screen) so each component is captured while all land in the one asset account.

---

## 2. Pass-through money (paying upfront for friends)

Both legs (your upfront payment **and** the repayments) hit your bank and get imported. Labeling them is **not** enough — a tagged deposit still counts as income. The clean solution is a **clearing account**, because money moving to/from your own account is a **transfer** and therefore invisible to income/expense.

### One-time setup

1. **Accounts → Asset accounts → Create new asset account**.
2. Name it e.g. **"Owed to me"** (a clearing / suspense account). Account role can be a normal default asset account.

### When you pay upfront (e.g. the full group concert ticket)

Edit the imported withdrawal into a **split transaction** (open the transaction → **Edit** → add a split with the "+"):

- **Split 1** = *your* share → normal expense account + real category/budget. This is your TRUE spend.
- **Split 2** = the friends' share → make it a **transfer** to **"Owed to me"**.

> The importer cannot split a transaction, so this split is a manual step.

### When friends pay you back

The repayment imports as a deposit. Convert it to a **transfer** from "Owed to me" back into your checking account — either manually (edit → change type to Transfer) or automatically with a **rule** (see §5).

### Verify nothing is dangling

- The **balance of "Owed to me"** is your live ledger of who still owes you.
- It **returns to zero** when everyone has settled. A non-zero balance = someone still owes you.
- Optional extras for auditing individual pairs:
  - **Tag** both legs (e.g. `reimbursement`) — review via **Reports → tag report** or the tag overview.
  - **Link** the repayment to the original with **"Is reimbursed by"** (on a transaction page → **Link transaction**). Links are purely informational — they do **not** net anything out.

### What does NOT work (don't bother)

- A "Reimbursements" **category** alone — labels only, still counts as income/expense.
- A **tag** alone — labels only.
- An **"exclude from reports" toggle** — does not exist.

---

## 3. Monthly contracts

Three different Firefly features. Pick by the nature of the obligation.

| Feature | Menu | Creates transactions? | Use for |
|---------|------|----------------------|---------|
| **Subscription (Bill)** | Automation → Subscriptions | No — only *matches* existing withdrawals | "Did my expected recurring payment arrive?" |
| **Recurring transaction** | Automation → Recurring transactions | **Yes** — auto-creates on a schedule (needs cron) | Generating entries when you do **not** import |
| **Liability** | Accounts → Liabilities | No — you post against it | Debts/loans/mortgage in net worth |

> Because your bot **imports** real transactions, prefer **Subscriptions**, not recurring transactions. A recurring transaction would create a duplicate of the imported payment.

### 3a. Insurances → Subscriptions

1. **Automation → Subscriptions → Create subscription**.
2. Set: name (e.g. `Car insurance`), **repetition** = monthly, **minimum** / **maximum** amount bracketing the premium, first **expected date**, active = yes.
   - For a quarterly bill on a monthly base, use the **skip** field (e.g. skip 2).
3. On save, Firefly **redirects you to create a Rule** pre-filled to match the amount/description. **Save it**, then refine the trigger to match how your bot labels the payment (description, opposing IBAN, amount range).
4. Let the bot import. The rule **auto-links** each imported premium to the subscription. The dashboard **Subscriptions** box then shows paid vs. expected/overdue.

Notes:
- A subscription can only link to **withdrawals** (never deposits/transfers).
- The expected **date is cosmetic** — it just drives the "expect it around X" display.
- Subscriptions are **not** linked to budgets — set budgets separately (§3d).

### 3b. Mortgage → Liability

1. **Accounts → Liabilities → Create new liability**.
2. Type = **Mortgage** (Loan/Debt/Mortgage are just labels; behavior is identical).
3. Set the outstanding principal under **"I owe amount"**. This makes net worth start at −(balance) and the liability carry a negative balance — which is correct ("the money is already spent, e.g. on a house").
4. You may fill in **interest rate** / **period**, but these are **administrative only** — Firefly does **not** calculate interest or amortization. You enter the real numbers each month.

**Recording the monthly payment** (as one split transaction, or two transactions):

- **Principal portion** → a transfer/withdrawal from your asset account **into the Mortgage liability**. This reduces "amount due".
- **Interest portion** → a **withdrawal** from your asset account to an expense account `Mortgage interest`. This is a real expense; it does **not** touch the mortgage balance.

> If Firefly rejects a *transfer* into the liability, post the principal as a **withdrawal** from asset → liability instead; the balance/net-worth result is the same.

**Reporting tip:** use **separate categories** for `Mortgage principal` vs `Mortgage interest` (even under one budget) so reports break out the split.

As you pay principal, the liability balance moves toward 0 and net worth rises accordingly.

### 3c. Personal taxes

Choose by behavior:

- **Periodic tax bill you pay on a schedule** → model like an insurance: a **Subscription** + a `Taxes` category, matched to the imported payment.
- **Reserving money for a future bill** ("set money aside"):
  1. **Accounts → Asset accounts → Create** a savings account `Tax reserve`.
  2. **Transfer** money into it monthly (Transactions → Transfers).
  3. Optionally add a **piggy bank** on it as a goal (**Accounts → Piggy banks → Create**, tied to that asset account).
  4. Pay the bill later as a **withdrawal** from `Tax reserve` to a `Tax authority` expense account, category `Taxes`.

  Piggy bank caveats: a piggy bank is a virtual envelope on a real asset account — it does **not** move money or change balances, **only works with transfers**, and is **not** part of "left to spend" (only budgets are). Use it for visualizing a goal, not for enforcing a limit.
- **Tax refunds** → a **deposit** from a `Tax authority` revenue account. (An expense account and revenue account may share the same name.)

### 3d. How Bills, Categories, and Budgets fit together

They are **independent, additive layers** — attach all three to one transaction:

- **Subscription (Bill)** → answers "did the expected payment arrive and was it in range?" Not connected to budgets.
- **Category** → what *kind* of cost it is (`Insurance`, `Mortgage interest`, `Taxes`). Drives spend-by-category reports; no limit.
- **Budget** → a spending **envelope with a limit** per period; the only one that feeds "left to spend".

Recommended: give each fixed cost a **Subscription** (did-it-happen), a **Category** (what it is), and put recurring fixed costs into a **Budget** if you want them counted in "left to spend".

---

## 4. Should the business bank account be one of your own accounts?

**Recommendation: No — keep it out and model the business as an external revenue account.**

### Why NOT an own asset account

If you add the business account as your **own asset account**, Firefly needs every movement in/out of it to keep the balance correct. But your business transactions are **not imported** — so the only entries would be the moments it pays you (transfers **out**). The account only ever gets debited, never credited with the real client revenue → its balance marches **negative**, and since net worth sums asset balances, it **drags your whole net worth down** and corrupts reports. A half-imported asset account is worse than none. *This is the negative-balance effect you observed.*

### Trade-offs

| Approach | Net worth | Income reports | Maintenance | Verdict |
|----------|-----------|----------------|-------------|---------|
| Business as your own **asset** account (not imported) | Wrong — goes negative | Distorted | High & still wrong | **Avoid** |
| Business as external **revenue** account | Correct (personal) | Truthful | Low | **Recommended** |
| Business as a **liability ("owed to me")** | Includes business cash, no negative drain | Payments show as deposits | Medium (manual upkeep) | Optional |
| Separate asset account **with full import** | Correct | Full | High | Only if you import business data |

### Middle ground (optional)

If you specifically want the cash the business holds *for you* to appear in net worth, model the business as a **Liability of type "Debt — owed to me"** (Accounts → Liabilities). Payments to you become "deposit from the liability" (reducing what it owes you); the balance can swing without the one-directional drain. Cost: you maintain the "owed" figure manually. For most purposes the plain revenue-account approach is simpler.

### When the business starts paying you monthly

- **Recommended setup (revenue account):** the monthly draw is just a **deposit** from `<Business> (owner's draw)` — counts as income like salary. Automate detection with a **rule**, and/or a **recurring transaction** only if it is *not* covered by the importer.
- **Liability route:** "deposit from the liability".
- It would only be a **transfer** (not income) if the business were a fully-imported asset account — which, per above, you should not do.

---

## 5. Working with the importer/bot

Rules can do most of the cleanup automatically. **Automation → Rules → Create rule** (group them with **Rule groups**).

Useful rule **triggers**: description contains, amount is/over/under, opposing account/IBAN, "transaction is created". Strict (ALL) vs non-strict (ANY) mode.

Useful rule **actions**:

- Set **category** / **budget** / **tag(s)**.
- Set **source** or **destination** account.
- **Convert** the transaction to a **deposit / transfer / withdrawal**.
- Set description / notes.

Enable **"apply rules"** on every import run so these fire unattended (importer config `"rules": true`, or API `"apply_rules": true`). You can also auto-tag every imported transaction with the importer's **custom import tag** feature.

### Concrete rules to create

- **Salary**: trigger on the employer's description/IBAN → set source = `Employer`, category `Salary`.
- **Business draw**: trigger on the business transfer description → set source = `<Business> (owner's draw)`, category `Business income`.
- **Reimbursements (incoming)**: trigger on the friend's name / payment-app memo / known IBAN → **convert to transfer**, set destination = `Owed to me`, add tag `reimbursement`.
- **Insurances / taxes**: the rule auto-generated when you create each Subscription handles linking; refine its trigger to your bot's labels.

### Importer limitations (manual steps remain)

- The importer **cannot split** a transaction → the upfront-payment split in §2 is manual.
- The importer **cannot create links or merge** transactions → "Is reimbursed by" links are manual.
- If a rule **clears all tags**, it loses to the importer's custom import tag (the import tag is added *after* creation) — mind the ordering.

---

## 6. Quick reference: where each thing goes

| Real-world thing | Firefly model | Transaction type | Menu |
|------------------|---------------|------------------|------|
| Salary | Deposit from `Employer` revenue account | Deposit | Transactions → Deposits |
| Business pays you | Deposit from `<Business> (owner's draw)` | Deposit | Transactions → Deposits |
| Pay upfront for friends | Split: your share = expense; their share = transfer to `Owed to me` | Withdrawal + Transfer | edit imported txn |
| Friend repays you | Transfer from `Owed to me` | Transfer | rule / edit imported txn |
| Insurance premium | Subscription + matching rule | Withdrawal (matched) | Automation → Subscriptions |
| Mortgage payment | Principal → into Mortgage liability; interest → expense | Transfer/withdrawal + Withdrawal | Accounts → Liabilities |
| Tax bill | Subscription, or withdrawal from `Tax reserve` | Withdrawal | Automation → Subscriptions |
| Tax reserve | Savings asset account (+ optional piggy bank) | Transfer in | Accounts → Asset / Piggy banks |
| Tax refund | Deposit from `Tax authority` revenue account | Deposit | Transactions → Deposits |
| Business account itself | External **revenue account** (not an asset account) | — | Accounts → Revenue accounts |

---

## Sources

Official Firefly III documentation (docs.firefly-iii.org) and project GitHub discussions/issues:

- Transactions & types: `/explanation/financial-concepts/transactions/`, `/references/firefly-iii/transaction-types/`
- Accounts & account types: `/explanation/financial-concepts/accounts/`, `/references/firefly-iii/account-types/`
- Liabilities: `/explanation/financial-concepts/liabilities/`, `/how-to/firefly-iii/finances/liabilities/`, mortgage tutorial `/tutorials/finances/mortgage/`
- Subscriptions (bills): `/explanation/financial-concepts/subscriptions/`, `/how-to/firefly-iii/finances/subscriptions/`
- Recurring transactions: `/explanation/financial-concepts/recurring/`, `/how-to/firefly-iii/finances/recurring/`
- Budgets, categories, tags ("what to use"): `/explanation/financial-concepts/budgets/`, `/explanation/data-classification/what-to-use/`
- Piggy banks: `/explanation/financial-concepts/piggy-banks/`
- Reports & refunds: `/how-to/firefly-iii/finances/reports/`, `/tutorials/finances/refund/`
- Rules & importer: `/references/firefly-iii/rule-actions/`, `/how-to/firefly-iii/features/rules/`, `/how-to/data-importer/import/file/`, `/how-to/data-importer/advanced/custom-import-tag/`
- Community: GitHub discussions #10372 (partial refunds with friends), #10810 (importer cannot split/link); issues #616 (links), #4268 & #1435 (exclude-from-reports — never built), #1992 (taxes via budgets/piggy banks).
