# Contributing

Contributions are welcome, especially synthetic fixtures for newly observed Apple
schema variants, decoding improvements, accessibility fixes, and clearer setup
instructions.

## Privacy rules

Never commit or attach real `chat.db`, `CallHistory.storedata`, exports, message
text, handles, phone numbers, contact names, attachments, screenshots, backup
passwords, `.env` files, or machine-specific evidence paths. Use synthetic data
and reserved example phone numbers.

Before opening a pull request:

```bash
python3 scripts/privacy_scan.py
python3 -m unittest discover -s tests
git diff --check
git status --short
```

Also inspect the staged file list and diff. Do not use real evidence for automated
tests. Temporary databases and outputs must use `tempfile` and be cleaned up.
The automated scan catches common identifiers and secret formats, but it cannot
decide whether an ordinary name or prose passage came from a real matter. Manually
review every staged text file as well.

## Compatibility changes

Apple schemas are private and can change. When adding support for a new variant:

- keep existing fields and raw values available where practical;
- treat undocumented enum labels as cautious operator aids;
- add a minimal synthetic schema fixture;
- describe the source category and OS version without identifying a person;
- avoid logging record bodies or participant identifiers; and
- update `docs/COMPATIBILITY.md` only for versions actually exercised.

## Pull requests

Keep changes focused. Explain user-visible output changes, test coverage, privacy
impact, and any backward-compatibility risk. Generated evidence packages must not
be included in a pull request.
