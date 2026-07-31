# Sandboxing

Transactional isolation and process sandboxing solve different problems. A Git transaction keeps prepared changes out of the source checkout. A sandbox limits what agent-launched processes can access while working in the isolated checkout.

## Check a backend

Inspect prerequisites, then run the behavioral probe:

```bash
mca doctor --cwd /path/to/repo --sandbox auto --provider auto
mca sandbox probe --sandbox auto
```

`doctor` performs static prerequisite checks. Coding runs perform the authoritative backend startup check. Doctor checks whether a provider key is present without printing its value and inspects private env-file metadata without reading the file.

The probe creates disposable data and tests a workspace write plus backend-specific outside-write, Unix-socket, and network boundaries. It prints one `[PASS]` or `[FAIL]` result per check and rejects `--sandbox none`, which cannot demonstrate isolation. A pass is evidence for these bounded checks, not proof that arbitrary untrusted code is safe.

## Backends

| Backend | Enforced boundary | Important limit |
| --- | --- | --- |
| Linux `bwrap` | Unshares namespaces, keeps the host root read-only, exposes only the workspace and private runtime tree as writable host paths, and supplies private `/run`, `/tmp`, home, `/dev`, and `/proc` views. | Relies on the host kernel and installed Bubblewrap. |
| macOS `sandbox-exec` | Denies network and default writes, hides the real home except for a workspace below it, and limits writes to the workspace and private runtime tree. | It is an OS policy profile, not a PID namespace, cgroup, or container boundary. |
| Docker | Uses no network, a read-only capability-free container, resource limits, one writable workspace bind, and a private size-limited `/tmp`. | Relies on a trusted daemon, image, host kernel, and configuration. Images need `/bin/sh`; the probe also needs `python3`. |
| Windows | Uses Docker when `--sandbox auto` is selected. | Windows has no native backend; `--sandbox none` explicitly disables process isolation. |

Native process-group cleanup after timeout, interruption, or exceptions is best effort. Bubblewrap's PID namespace and Docker's container boundary provide stronger descendant containment, but no backend provides an absolute OS or process-containment guarantee.

The probe uses stricter backend-specific evidence. Native backends must read an exact host sentinel and report denial only for `EPERM`, `EACCES`, or `EROFS`. Docker must not see the sentinel and must report the `ST_RDONLY` mount flag for `/`. Bubblewrap and Docker must hide controlled and known host Unix sockets; `sandbox-exec` may expose a socket path only if connection remains denied. Network checking first attempts a no-packet UDP connection to a TEST-NET address and falls back to a controlled loopback TCP denial check when no outbound route can be obtained.

These controls are defense in depth, not a guarantee that an untrusted repository, command, dependency, image, host, or provider is safe. Do not run the tool in a workspace containing production credentials. See the [security policy](../SECURITY.md) for the complete threat model.
