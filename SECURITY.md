# Security

tenx-mess deletes files, so a bug that makes it delete the wrong thing is a security problem.

## Reporting

Please don't open a public issue. Report it privately through GitHub instead: [report a vulnerability](https://github.com/muratcakmak/tenx-mess/security/advisories/new). You'll get a reply on the advisory, and the fix and credit are worked out there.

## What counts

- `clean.py` deletes, kills or erases anything that is not in the plan the user approved.
- The plan hash, the approval check or the run-time re-checks can be bypassed.
- The report, the page or the approval can change which program runs or which flags it gets. Paths and IDs from the report are expected arguments, and each is checked again at run time.
- A protected path is deleted, or a symlink is followed out of a target folder.
- The confirmation page or its database can be used to approve a plan the owner didn't approve.

Wrong tiers, wrong sizes and missed targets are ordinary bugs. Use the [bug template](https://github.com/muratcakmak/tenx-mess/issues/new?template=bug_report.yml) for those.

## Supported versions

Only the latest release gets fixes.
