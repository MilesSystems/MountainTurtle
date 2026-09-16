# Harvester SFTP test drive

This dedicated fixture gives Mountain Turtle a small writable SFTP server for
integration testing. It contains test data only.

| Connection field | Value |
| --- | --- |
| Name | Harvester SFTP Test |
| Server | `192.168.1.100` |
| Port | `30223` |
| Username | `turtle` |
| Remote folder | `/upload` |
| Authentication | Private key file |
| Private key | `~/Library/Application Support/Mountain Turtle/sftp-test/id_ed25519` |
| Known hosts | `~/Library/Application Support/Mountain Turtle/sftp-test/known_hosts` |
| Initial access | Read only; enable writing for deliberate test uploads |

Port `30222` was already used by `default/kali-ssh`, so this fixture uses `30223`.
It has no ingress, load balancer, public DNS record, or external credentials.
The network policy admits SSH only from `192.168.1.0/24` and denies new outbound
connections. NodePort availability still depends on the LAN and cluster routing.
The test pod is pinned to `r640.assessorly.com`, and the service uses
`externalTrafficPolicy: Local` so the `.100` endpoint preserves the client's LAN
address. Other nodes are not alternate endpoints for this fixture.

## Deploy or restore

From this repository, run:

```sh
./deploy/sftp-test/deploy.sh /Users/rtm/.kube/local.yaml
kubectl --kubeconfig /Users/rtm/.kube/local.yaml \
  -n mountain-turtle-test rollout status deployment/sftp
```

The script generates separate client and server SSH keys on this Mac if they
are absent. It reuses existing keys on subsequent runs. Private keys stay outside
the repository; only server private keys are uploaded to the fixture's Kubernetes
Secret. Client authorization uses a public-key ConfigMap. The local `known_hosts`
file comes from the provisioned host public key, rather than trusting a network
key scan. Keep this directory to preserve the fixture's identity during recovery.

The host key fingerprint for the fixture created on September 16, 2026 is:

```text
ED25519 SHA256:Exipw/CwxtfiTEavlwwtyyBqcX9ReA6fUznzppm3TrQ
```

The image is the official [atmoz/sftp](https://github.com/atmoz/sftp) Debian image,
pinned to the registry manifest resolved on September 16, 2026:

```text
atmoz/sftp@sha256:a5a0081d3538c89a3d5addc321589916b85760ac63328449beb3a005fe9598b6
```

OpenSSH accepts only the dedicated `turtle` public key, forces `internal-sftp`,
chroots the user into `/home/turtle`, and disables shell, password login, terminal,
agent forwarding, and port forwarding. The writable PVC appears as `/upload`.
OpenSSH runs its supervisor as root to change identity and chroot; the container
is not privileged, does not mount host paths, drops unused capabilities, and has
CPU and memory limits. Its service-account token is not mounted.

## Resources

All names are within namespace `mountain-turtle-test`:

- Deployment `sftp`, one replica with a recreate update strategy.
- Service `sftp`, TCP `22`, NodePort `30223`.
- PVC `sftp-data`, `1Gi`, `ReadWriteOnce`, storage class `harvester-longhorn-2r`.
- Secret `sftp-host-keys`, generated server private keys.
- ConfigMaps `sftp-config` and `sftp-authorized-keys`.
- NetworkPolicy `sftp-lan-only`.

Private keys and known hosts on the Mac have mode `0600`, inside a `0700`
directory. Deployment files contain no private key material.

## Manual check

```sh
sftp -P 30223 \
  -i "$HOME/Library/Application Support/Mountain Turtle/sftp-test/id_ed25519" \
  -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=yes \
  -o "UserKnownHostsFile=$HOME/Library/Application Support/Mountain Turtle/sftp-test/known_hosts" \
  turtle@192.168.1.100
```

The initial folder is `/upload`. Use unique disposable filenames for upload,
rename, download, and delete checks. Switch Mountain Turtle to read-only again
after testing writes. The storage is a test fixture, not a backup destination.

## Verified September 16, 2026

The live fixture passed a 512 KiB upload, byte-for-byte download, rename, and
deletion check. It rejected an SSH shell command. `Welcome.txt` and a 4 MiB sample
file were then added, the deployment was restarted, and both files were read back
successfully using the same strictly verified server key. Deployment `sftp` was
`1/1` Ready on `r640.assessorly.com`; PVC `sftp-data` was Bound to
`pvc-a19171c3-7095-49a8-88ca-7e234165ce8f`.

SFTP's filesystem-statistics extension reported 1,020,702,720 usable bytes total,
4,227,072 bytes used, and 1,016,475,648 bytes free after seeding. This is filesystem
capacity after formatting, rather than the PVC's nominal 1 GiB allocation. The
query succeeded with remote shell commands disabled. These numbers are a recorded
verification snapshot, not a live capacity guarantee.

## Teardown

Disconnect and remove **Harvester SFTP Test** from Mountain Turtle first.
Deleting the dedicated namespace deletes the fixture and its PVC, including all
test files; the Longhorn storage class uses a delete reclaim policy:

```sh
kubectl --kubeconfig /Users/rtm/.kube/local.yaml delete namespace mountain-turtle-test
```

The local key directory is retained deliberately. Remove that exact directory
separately if the fixture will not be restored. No other namespace is used.
