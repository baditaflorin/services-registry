# Inspecting a standby builder from a workstation

The workstation `fleet-runner` shim normally forwards commands to the primary
builder. If that host is down, the shim bounds SSH connection setup to 10 seconds
and uses SSH keepalives on an established connection. A reachable remote command
can still run for as long as it needs; this is not a command execution timeout.

To inspect an explicitly configured standby without reaching the primary:

```sh
FLEET_STANDBY_HOST=<trusted-ssh-alias> fleet-runner --standby-status
```

This read-only command reports the recorded builder role and build identity,
whether the standby binary is present, and whether two reconciliation timers
are enabled. It sends no fleet credentials, does not start the binary, and
does not acquire a lease or promote a builder. The SSH alias must already have
working authentication and host-key verification. A successful report proves
only that the target could be inspected: it does not establish that the binary
is current, that the pool is healthy, or that a deploy can succeed. The build
identity is the installation record, not an independent binary verification.

There is no automatic fallback to the standby: an old binary, missing
credentials, stale workspace, or absent fencing can make an automatic switch
unsafe. Follow the private builder recovery runbook for any promotion. A normal
command still targets the primary and will report an SSH connection failure if
that host is unavailable.
