# Security policy

## Supported versions

This project is pre-release. Security updates apply to the latest reviewed
commit only.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for this repository. Do not open a
public issue containing exploit details, credentials, customer data, prompts,
responses, traces, or evaluation dataset rows.

## Security principles

- Use OIDC; never store static Azure credentials.
- Pin privileged execution to an exact reviewed commit.
- Keep customer policy and configuration reviewable in the customer repository.
- Treat issue content, candidate edits, and remote service responses as
  untrusted until validated.
- Keep optimization draft-only and deployment route-read-only.
