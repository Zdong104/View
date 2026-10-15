# Security Policy

## Supported versions

Only the latest release is supported.

## Reporting a vulnerability

Please **do not** open a public issue for security problems. Report privately through GitHub:
[Report a vulnerability](https://github.com/Zdong104/Preview/security/advisories/new).

Include the affected version (`preview-app --version`), how you installed it, and steps or a sample file to reproduce. Preview parses untrusted files (PDF, images, meshes, tables), so crashes and memory problems on crafted input are in scope. You can expect an initial response within about a week.

Note: touchpad signing reads `/dev/input` devices, which requires membership of the `input` group. That is by design; see the README.
