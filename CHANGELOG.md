# Changelog

## v1.0.0

- Added GitHub release packaging for online update bundles.
- Added an in-app updater that reads `latest.json`, verifies SHA256, downloads the update zip, and launches `update.bat`.
- Kept local runtime data, generated jobs, caches, and FFmpeg binaries out of the GitHub source repository.
