# secret-scan

Only a few of the fleet's repos scan for secrets in their own CI, so this scans them all from one place: gitleaks (pinned
image, same as the registry's CI) over `git archive origin/main` of every checkout in `/root/workspace` on Builder CT108,
one repo at a time. `gitleaks.toml` = default rules + `fb_`/`ak_` fleet key shapes + credentials in URLs; dependencies and
lock files are skipped. Reports are redacted: only repo, rule, file and line are ever printed.

- Weekly (Sunday 03:20 UTC): `secret-scan-weekly.timer`. Compares with `/var/lib/secret-scan/baseline.tsv` (repo, rule, file)
  and lists only NEW findings in the journal (`journalctl -u secret-scan-weekly`) and in `fleet_secret_scan_new_findings`.
  Alert on `fleet_secret_scan_new_findings > 0` and on `fleet_secret_scan_run_success == 0`.
- Accept a finding as a false positive (a test fixture, a scanner's own rule): append its `repo<TAB>rule<TAB>file` line from
  `/var/lib/secret-scan/new.txt` to `baseline.tsv`. A real secret is never baselined: remove it from the repo and rotate it.
- Pause: `touch /var/lib/secret-scan/PAUSE`.
- A scan of the checked-in tree cannot see history; a secret removed from main is still in history and must be rotated.
