# Security policy

## Reporting a vulnerability

Do not place private communications, Apple databases, exports, credentials,
backup passwords, phone numbers, or attachment samples in a public issue.

Use GitHub's private vulnerability reporting for this repository when available.
If it is unavailable, open a minimal issue requesting a private contact channel
without including exploit details or sensitive data.

## Data handling

The exporter is designed to run locally. It does not require a network connection
after dependencies are installed. Generated packages are not encrypted by the
tool; store and transfer them using controls appropriate for confidential legal
material.

Dependencies should be installed from the checked-in Python requirements and npm
lockfile. Review dependency updates before use in a sensitive acquisition.
