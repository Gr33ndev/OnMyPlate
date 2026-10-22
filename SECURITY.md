# Security policy

On My Plate handles access tokens for your issue trackers and runs a local web server, so security reports are very welcome.

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's private reporting instead: *Security → Report a vulnerability* on this repository. Include the On My Plate version (`VERSION` in `onmyplate.py`), your Python version and OS, and steps to reproduce.

You can expect a first reply within a week. Fixes are released as soon as they are ready, and reporters are credited in the changelog unless they prefer otherwise.

## Supported versions

Only the latest release receives security fixes.

## Scope

In scope, for example:

- Ways for a web page, another local user or process, or a malicious server response to read your tokens, tasks or ticket data, or to act through the local API.
- Ways to make On My Plate send anything other than read requests, or send tokens to an unexpected host.
- Ways to make a token end up in plain text on disk, in a log or in the browser.
- Script injection into the UI or into `onmyplate.todos.md`.

Out of scope:

- Attacks that require control of your user account or browser profile.
- Deliberately insecure settings: `verify_ssl: false` or `allow_insecure_http: true`.

## Design notes

The threat model and the protections against it are summarised in the *Security* section of the [README](README.md#security). The module docstring of `onmyplate.py` lists them as well.
