# Gmail Cleanup MVP

This is a small Google Apps Script that runs inside your Gmail account and moves matching threads to Trash in batches. It avoids the connector bottleneck where only 100 message IDs can be moved at a time through chat.

## Use It

1. Open https://script.google.com.
2. Create a new project.
3. Paste the contents of `tools/gmail-cleanup.gs`.
4. Run `startCleanup` once with `DRY_RUN = true`.
5. Check **Executions** or **Logs**.
6. Change `DRY_RUN` to `false`.
7. Run `startCleanup` again.

The script resumes automatically with a 1-minute trigger until enabled jobs are done.

## Defaults

Enabled:

```text
(category:promotions OR category:social) older_than:30d -is:starred -is:important -in:trash -in:spam
```

Available but disabled by default:

```text
category:updates older_than:180d -is:starred -is:important -in:trash -in:spam
```

Updates can include receipts, bank alerts, travel, bills, and account-security messages, so review that query before enabling it.

## Recovery

The script moves mail to Gmail Trash. It does not permanently delete messages immediately.
