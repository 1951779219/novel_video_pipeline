# Changelog

## v1.0.4

- Fixed duplicate job IDs when importing multiple TXT files in the same second, so queued batch imports no longer overwrite each other.

## v1.0.3

- Added task stop controls for running workers in the desktop GUI and WebUI.
- Running tasks can now be stopped before deletion, including process-tree termination on Windows.
- Deleting a running task now stops the worker first and retries directory removal.

## v1.0.2

- Added cache-busting to update manifest checks so GitHub `latest.json` does not get stuck on an older release.

## v1.0.1

- Added the public feedback entry for users to submit bugs and feature requests.
- Added GitHub Issue templates plus automatic triage labels for feedback issues.
- Added the feedback/update closed loop documentation.

## v1.0.0

- Added GitHub release packaging for online update bundles.
- Added an in-app updater that reads `latest.json`, verifies SHA256, downloads the update zip, and launches `update.bat`.
- Kept local runtime data, generated jobs, caches, and FFmpeg binaries out of the GitHub source repository.
