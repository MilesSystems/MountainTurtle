#!/bin/bash
set -euo pipefail
umask 077

fixture_directory=$(cd -- "$(dirname -- "$0")" && pwd)
fixture_keys="$HOME/Library/Application Support/Mountain Turtle/sftp-test"
fixture_kubeconfig=${1:-"$HOME/.kube/local.yaml"}
fixture_kubectl=(kubectl --kubeconfig "$fixture_kubeconfig")

fixture_existing_namespace=$("${fixture_kubectl[@]}" get namespace mountain-turtle-test --ignore-not-found \
    -o 'jsonpath={.metadata.name}{"|"}{.metadata.labels.app\.kubernetes\.io/part-of}')
if [[ -n "$fixture_existing_namespace" && "$fixture_existing_namespace" != 'mountain-turtle-test|mountain-turtle' ]]; then
    printf 'Refusing to change an existing namespace that is not labeled as a Mountain Turtle fixture.\n' >&2
    exit 1
fi

# Never replace an existing key: redeployment keeps both identities stable.
mkdir -p "$fixture_keys"
chmod 700 "$fixture_keys"
for fixture_key in id_ed25519 ssh_host_ed25519_key; do
    if [[ ! -f "$fixture_keys/$fixture_key" ]]; then
        ssh-keygen -q -t ed25519 -N '' -C "mountain-turtle-sftp-test" -f "$fixture_keys/$fixture_key"
    fi
    chmod 600 "$fixture_keys/$fixture_key"
done
if [[ ! -f "$fixture_keys/ssh_host_rsa_key" ]]; then
    ssh-keygen -q -t rsa -b 4096 -N '' -C "mountain-turtle-sftp-test-host" -f "$fixture_keys/ssh_host_rsa_key"
fi
chmod 600 "$fixture_keys/ssh_host_rsa_key"

# Trust the public half of the host key that we provision to this deployment,
# not an unauthenticated key collected from the network.
read -r fixture_algorithm fixture_public _ < "$fixture_keys/ssh_host_ed25519_key.pub"
printf '[192.168.1.100]:30223 %s %s\n' "$fixture_algorithm" "$fixture_public" > "$fixture_keys/known_hosts"
chmod 600 "$fixture_keys/known_hosts"

"${fixture_kubectl[@]}" apply -f "$fixture_directory/namespace.yaml"
"${fixture_kubectl[@]}" -n mountain-turtle-test create secret generic sftp-host-keys \
    --from-file="ssh_host_ed25519_key=$fixture_keys/ssh_host_ed25519_key" \
    --from-file="ssh_host_rsa_key=$fixture_keys/ssh_host_rsa_key" \
    --dry-run=client -o json |
    "${fixture_kubectl[@]}" apply --server-side --field-manager=mountain-turtle-sftp-fixture -f -
"${fixture_kubectl[@]}" -n mountain-turtle-test create configmap sftp-authorized-keys \
    --from-file="id_ed25519.pub=$fixture_keys/id_ed25519.pub" --dry-run=client -o json |
    "${fixture_kubectl[@]}" apply --server-side --field-manager=mountain-turtle-sftp-fixture -f -
"${fixture_kubectl[@]}" apply -f "$fixture_directory/workload.yaml"
printf '\nSFTP: turtle@192.168.1.100:30223, folder /upload\n'
printf 'Private key: %s/id_ed25519\nKnown hosts: %s/known_hosts\n' "$fixture_keys" "$fixture_keys"
printf 'Wait for readiness: kubectl --kubeconfig "%s" -n mountain-turtle-test rollout status deployment/sftp\n' "$fixture_kubeconfig"
