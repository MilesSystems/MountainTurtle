# Manual acceptance checklist

This is a checklist to execute, not a record of completed verification. Record
the app revision, macOS version, architecture, Python version, rclone version,
AWS CLI version when testing S3, and date with each run. Keep account IDs, temporary credentials,
SSO tokens, and private photo contents out of published evidence.

Completed initial-build checks are recorded separately in the
[2026-09-14 verification record](VERIFICATION-2026-09-14.md). The unchecked items
below remain a reusable checklist; they are not a claim that every check was run.

Use an explicitly selected S3 bucket in **read-only** mode with its existing
profile and region; do not discover unrelated buckets or perform write tests
there. For deliberate SFTP writes, use the dedicated
[Harvester fixture](../deploy/sftp-test/README.md) and unique disposable filenames.
That document records completed fixture checks separately from this checklist.

## Build and first launch

- [ ] Run `./scripts/build.sh`; verify it succeeds and produces
  `build/Mountain Turtle.app`.
- [ ] Run `./scripts/install.sh`; launch `~/Applications/Mountain Turtle.app`.
- [ ] Confirm the app opens without a terminal window and shows useful dependency
  status for Python and rclone; AWS CLI should be optional for SFTP-only use.
- [ ] Check labels, empty state, keyboard navigation, focus, and readable error
  text. Resize the window without clipping the main connection controls.
- [ ] Verify missing-dependency handling with a mocked tool-discovery result;
  do not uninstall the user's working tools.

## Saved connection and sign-in

- [ ] Add one selected S3 connection with the exact bucket, profile, and region.
  Confirm **Read only** starts enabled and auto-connect starts disabled.
- [ ] Try duplicate names, slash-containing names, and blank required fields.
  They should produce clear validation errors and no mount or remote mutation.
- [ ] Close and reopen the app; verify the saved settings remain.
- [ ] Sign in using the connection's AWS profile when needed. Confirm that
  authentication takes place through AWS and no tokens appear in app status,
  command output, or logs.
- [ ] With the connection disconnected, edit its local display name, then
  restore it. Confirm connected records cannot be edited or removed.

## SFTP connection and identity verification

- [ ] Add a connection with host, username, port, remote folder, and verified
  known-hosts file. Confirm read-only is enabled and auto-connect disabled.
- [ ] Check default port 22, blank home folder, absolute and home-relative paths;
  reject invalid ports, URL-style hostnames, traversal, and missing key files.
- [ ] With an isolated known-hosts fixture, verify unknown and changed host keys
  fail without a mount or automatic key enrollment. Preserve the real trusted file.
- [ ] Verify SSH-agent and private-key authentication against the test server.
  Confirm an SFTP-only account works without shell access.
- [ ] Test password save/edit using a disposable password-enabled fixture and
  the built app's Keychain helper. Confirm no secret appears in status, logs,
  argv, saved JSON, or rclone configuration.
- [ ] Confirm blank password edits preserve it for unchanged host/user/port,
  and changing those fields requires new input; folder/name edits preserve it.
- [ ] Confirm SFTP connections show server details and no AWS sign-in or separate
  photo-browser buttons. A stale photo deep link should explain Finder browsing.
- [ ] Compare a known fixture file read through the Finder mount with a direct
  SFTP read. If testing writes, verify upload/readback/rename/delete only in the
  dedicated fixture and restore read-only afterward.

## Real Finder volume and one photo read

- [ ] Connect. Confirm the UI reaches connected and the operating system shows
  a real `nfs` mount under `~/Mountain Turtle/<connection name>`.
- [ ] Confirm the associated NFS listener is bound to `127.0.0.1`, not
  `0.0.0.0` or a LAN address.
- [ ] With Finder's Connected servers options enabled, verify the volume appears
  as a server/volume and its local custom icon is visible.
- [ ] Open an existing small photo folder and preview one known photo. Record
  only its key or a redacted identifier, size, and the observed result.
- [ ] Read a bounded portion or the same small file through the mount and through
  an authenticated read of its known S3 key; compare bytes or a digest. This
  proves content access, not just that a drive icon exists.
- [ ] Verify the mount configuration enforces read-only mode. Do not attempt a
  live write to demonstrate it; test rejected writes against a local fixture.
- [ ] Confirm connecting did not initiate a recursive bucket scan, upload an
  icon, mount other buckets, or start opening every photo for thumbnails.

## Ejection and failure handling

- [ ] Eject a normally idle volume; verify both the UI and mount table show it
  disconnected and the saved connection is retained.
- [ ] Reconnect, hold a file or directory open using a local test process, and
  request disconnect. Verify a busy failure leaves a usable mounted volume and
  explains what to close. Release the handle and disconnect successfully.
- [ ] Simulate unavailable SSO credentials with a mocked AWS command response.
  Verify a needs-login state, no browser loop, and no false empty-bucket result.
- [ ] Simulate a stopped child process and temporary network errors using local
  fixtures; verify the service reports and recovers without duplicate mounts.
- [ ] Attempt removal while connected; it must be rejected. Remove a
  disconnected throwaway local record; verify no S3 delete call is issued.

## Login startup

- [ ] Enable login startup on the installed app and auto-connect on the test
  connection. Verify the per-user LaunchAgent points into the installed app.
- [ ] Disable login startup while the test volume is connected; verify the
  current volume stays connected.
- [ ] Re-enable it and verify reconnect after an actual logout/login or reboot
  when the user is ready for that disruption. If no restart was performed,
  report registration as verified and reboot reconnect as **unverified**.
- [ ] If the SSO session has expired, verify the app explains that sign-in is
  required instead of claiming unconditional reconnect success.

## Local cache and write-path checks

- [ ] Use temporary directories, a fake rclone process, and mocked S3 responses
  for write-mode tests. Never point those tests at the live photography bucket.
- [ ] Check delayed writes, failed uploads, retry state, and busy shutdown while
  preserving cached data. Test success and failure separately.
- [ ] Check that read-only settings reach rclone and that mutation paths are
  rejected by the local fixture.
- [ ] Verify test isolation prevents AWS or rclone subprocesses from reaching an
  unintended remote, even if the developer has valid profiles configured.
- [ ] Report exactly which write/cache tests exist and passed. Do not infer live
  durability, upload success, or complete offline availability from mock results.

## Local metrics and remote storage

- [ ] Open **View metrics** on both backends. Check keyboard access and readable
  labels at the actual 930×740 dashboard size; inspect graphs after scrolling.
- [ ] Compare transferred bytes, errors, cache use, and uploads against the
  mount's authenticated local statistics. Do not label combined traffic as
  download-only or as the provider's billable bandwidth.
- [ ] Generate a bounded fixture read, wait for consecutive samples, and verify
  throughput changes. First readings, resets, and long gaps must remain unknown.
- [ ] Check disconnected, partial, failed, zero, and unavailable values. Missing
  values must not become zero. Closing the dashboard must stop local polling.
- [ ] Verify local history is bounded to 720 observations in 24 hours and that
  credentials and filenames are absent from its JSON.
- [ ] On S3, compare source dates, total bytes, object count, and classes with a
  read-only CloudWatch response. Verify the 30-day graphs leave missing days
  empty and distinguish old/partial data from a current complete measurement.
- [ ] Test denied CloudWatch access and expired AWS sign-in without hiding local
  metrics or claiming empty storage. Remote metrics must work while ejected.
- [ ] Check automatic public regional pricing against the official price list,
  including tiers. The source/date and assumptions must be visible. Unsupported
  nonzero classes must retain an unknown total and label any known subtotal.
- [ ] Enter a manual blended rate and change the growth assumption. Verify cost
  recalculates locally, persists the per-drive rate, and reverts to automatic
  pricing when cleared. Check zero and invalid rates. Scenarios must be labeled
  as estimates, with requests/transfer and other excluded charges stated.
- [ ] On SFTP, compare server total/used/free values with its filesystem-statistics
  response. Confirm scope is the remote filesystem, not the selected folder or
  this Mac's cache. Check its sampled history and measurement timestamp.
- [ ] Verify unsupported SFTP capacity returns unknown without a recursive scan
  or shell fallback. Do not infer a cloud price from filesystem capacity.
- [ ] Confirm remote storage loads once on dashboard opening and on manual
  refresh, with no recurring cloud/server-capacity polling or scan.

## Evidence to retain

Record build output, local test results, a redacted status response, the relevant
mount-table row, listener binding, and the known-photo byte comparison. Capture
an actual app/Finder screenshot only after the view has been verified. List any
unexecuted checks, especially reboot reconnect, untested authentication modes, unsupported remote
capacity, and any live write behavior not covered by the dedicated fixture.
