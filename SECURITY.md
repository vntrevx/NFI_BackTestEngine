# Security policy

## Supported versions

Only the latest published release receives security fixes.

## Reporting

Do not open a public issue for a credential leak or code-execution vulnerability. Use
GitHub's private vulnerability reporting for this repository.

Strategy files are executable Python and must be treated as trusted local code. The static
preflight detects unsupported constructs for compilation; it is not a malware sandbox.
Configuration artifacts written by the research runner contain blanked credential fields.
