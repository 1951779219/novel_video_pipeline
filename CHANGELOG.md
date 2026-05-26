# Changelog

## v1.0.6

- Improved OpenAI-compatible image generation errors so HTTP failures include a redacted response summary and a model hint when an unsupported `gpt-image-*` model may be configured.

## v1.0.5

- Added a user-facing production-stage display in the desktop GUI and WebUI so users can see whether a task is reading text, cleaning, generating speech, generating images, composing video, or uploading.
- Included the readable production stage in task status data to make stuck or long-running jobs easier to diagnose.

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
