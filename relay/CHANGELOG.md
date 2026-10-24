# Changelog

All notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). The add-on (`relay/`) and the
integration (`custom_components/icomm_local/`) share one version.

`relay/CHANGELOG.md` is a copy of this file: the Home Assistant add-on update
dialog reads it from the add-on folder. CI fails if the two differ.

## [Unreleased]

### Changed
- Add-on dependency floors raised to what CI and image builds already
  installed: aiohttp 3.14.5, pycryptodome 4.1.0. pycryptodome 4.0's breaking
  changes don't touch AES-CBC, the only primitive the relay uses.
- CI actions: checkout 7, CodeQL 4, setup-python 7, gitleaks-action 3, docker build-push 7,
  setup-buildx 4, setup-qemu 4. Dev tools: pytest 9, pytest-asyncio 1.4,
  ruff 0.17.
- Dependabot opens one grouped PR per ecosystem.

## [0.4.6] - 2026-10-24

### Fixed
- During an Ayla outage, every module command poll got a 500 instead of the
  relay's synthetic empty reply (a bad `web.Response` keyword, present since
  the first release). Queued writes could not ride the cloud path while Ayla was
  down. LAN delivery was unaffected.
- A non-JSON reply from the relay (a proxy's HTML error page, a relay
  mid-restart) now gives a readable error in services, setup and the config
  flow instead of a raw `JSONDecodeError`.

### Removed
- The read-back retry from 0.4.4. Live, the module ignored every retried read
  as well (3 of 6 writes, 6 retries), and confirmation still arrived from its
  own report at 4–5 s. The reasoning is in `docs/LAN_MODE.md`.

### Added
- `CHANGELOG.md`, mirrored to `relay/CHANGELOG.md` for the add-on update dialog.
- Test coverage raised to 99% (relay) and 100% (integration); the new tests
  found both fixes above.
- CI: coverage gate (95%) for the relay and the integration, a release workflow
  driven by tags, gitleaks, CodeQL, Dependabot, and multi-arch builds of the
  add-on image.

## [0.4.5] - 2026-10-24

### Fixed
- Stalled LAN polling: if the module keeps accepting heartbeats but stops
  polling for commands (seen once, right after an app write: 5+ minutes with no
  polls), the relay re-registers after `LAN_STALL_AFTER` (default 60 s; `0`
  disables). The fresh session resumes polling.

## [0.4.4] - 2026-10-24

### Added
- A read-back retry at +1.5 s / +3.0 s after a write ack. Removed in 0.4.6
  because it did not work.

## [0.4.3] - 2026-10-24

### Fixed
- A write queued while the module was re-keying its LAN session waited a full
  ~10 s heartbeat. The relay now wakes the module as soon as the new session
  has been verified.

## [0.4.2] - 2026-10-24

### Added
- Wake on write: queueing a write sends an immediate heartbeat with
  `notify=1`, so the module fetches commands at once. Delivery dropped from up
  to 10 s to ~0.1 s.
- *Last app write* sensor: writes made through the A.O. Smith app or its API
  are logged and exposed, including whether a queued local write displaced one.

### Fixed
- Since 0.3.1, writes injected on the cloud fallback path were silently
  discarded unless a LAN-key push was armed, though they still counted as
  delivered.

## [0.4.1] - 2026-10-24

### Fixed
- Heat deficit uses the `energy_storage` device class (`energy` with
  `state_class: measurement` is invalid in Home Assistant).

## [0.4.0] - 2026-10-24

### Added
- *Hot water available* (%), the app's 0/50/100 figure computed locally from
  the controller's energy-deficit counter.
- *Heat deficit* (Wh) and *Water, tank middle* sensors.

## [0.3.9] - 2026-10-24

### Fixed
- Untrusted peers can no longer influence the learned reporting interval.
- Rejected writes retire after 15 minutes even below the delivery cap.
- Late confirmation applies only to writes delivered within the last hour.

## [0.3.8] - 2026-10-24

### Security
- Peer allowlist on the module-facing ports. Any LAN host could previously
  fetch the relay's Local Connect key from `lan.json`. New `trusted_peers`
  option.
- The LAN CBC chain advances only after the HMAC verifies.

### Fixed
- Write queue: oldest-first selection, 3 deliveries in total across LAN and
  cloud, retirement of exhausted writes, new `rejected` status.
- The LAN heartbeat loop survives any exception.

### Changed
- README rewritten for new users.

## [0.3.7] - 2026-10-24

### Changed
- Stale data shows a Repairs warning instead of making the entities
  unavailable. Home Assistant silently drops writes to unavailable entities.

## [0.3.6] - 2026-10-24

### Added
- Write status lifecycle: queued → delivered → acknowledged → confirmed /
  not_applied (0.3.5).
- Keep-alive telemetry reads every 5 minutes while a LAN session is up.

## [0.3.4] - 2026-10-24

### Added
- Automatic repair of a module holding a foreign LAN key (Ayla's
  `PUT lanip.json`).
- Active telemetry read-back after write acks and at session start.

## [0.3.0] - 2026-10-23

### Added
- Local writes and telemetry over Ayla Local Connect, with no A.O. Smith
  account.

### Removed
- The cloud doorbell and its A.O. Smith credentials.

## [0.2.3] - 2026-10-23

### Fixed
- The relay forwards every module path verbatim. `/dsns/` previously got a 404.

## [0.2.2] - 2026-10-23

### Added
- Mode enum sensor and the router steering guide.

[Unreleased]: https://github.com/ericboehs/ha-icomm-local/compare/v0.4.6...HEAD
[0.4.6]: https://github.com/ericboehs/ha-icomm-local/compare/v0.4.5...v0.4.6
[0.4.5]: https://github.com/ericboehs/ha-icomm-local/compare/v0.4.4...v0.4.5
[0.4.4]: https://github.com/ericboehs/ha-icomm-local/compare/v0.4.3...v0.4.4
[0.4.3]: https://github.com/ericboehs/ha-icomm-local/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/ericboehs/ha-icomm-local/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/ericboehs/ha-icomm-local/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/ericboehs/ha-icomm-local/compare/v0.3.9...v0.4.0
[0.3.9]: https://github.com/ericboehs/ha-icomm-local/compare/v0.3.8...v0.3.9
[0.3.8]: https://github.com/ericboehs/ha-icomm-local/compare/v0.3.7...v0.3.8
[0.3.7]: https://github.com/ericboehs/ha-icomm-local/compare/v0.3.6...v0.3.7
[0.3.6]: https://github.com/ericboehs/ha-icomm-local/compare/v0.3.4...v0.3.6
[0.3.4]: https://github.com/ericboehs/ha-icomm-local/compare/v0.3.0...v0.3.4
[0.3.0]: https://github.com/ericboehs/ha-icomm-local/compare/v0.2.3...v0.3.0
[0.2.3]: https://github.com/ericboehs/ha-icomm-local/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/ericboehs/ha-icomm-local/releases/tag/v0.2.2
