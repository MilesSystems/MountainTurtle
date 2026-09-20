# Releasing Mountain Turtle

Git pushes upload source. A versioned GitHub Release adds the downloadable app,
release notes, and signed update feed. Every completed Codex change reaching
`origin/main` must also publish its versioned release, including documentation
and repository instruction changes. `AGENTS.md` provides standing authorization
to prepare, sign, tag, publish, and verify that release without another
publication confirmation unless the user explicitly pauses or limits it.
Until Developer ID signing and notarization are configured, continue publishing
explicitly labelled, Apple-signed Previews through the existing update channel.

The app uses [Sparkle 2.10.0](https://sparkle-project.org/documentation/) to check
the GitHub feed at
`https://github.com/MilesSystems/MountainTurtle/releases/latest/download/appcast.xml`.
Checks run automatically, and users can choose **Mountain Turtle → Check for
Updates…**. Sparkle asks before downloading, installing, and relaunching. The
app verifies the signed feed and validates each Ed25519 signed archive before
extracting it. Version 0.7.0 is the first app containing this updater; older
versions need one manual installation.

## Signing setup

`scripts/fetch-sparkle.sh` downloads the official framework and tools and verifies
a pinned SHA-256. The framework and all helper executables are embedded and
signed from the inside out. Sparkle's copyright and dependency notices are in
`THIRD_PARTY_NOTICES.md` and are included in the app.

The private update-signing key stays in the macOS login Keychain under account
`io.mountainturtle.app`. Only its public key is tracked in
`Resources/SparklePublicKey.txt`. On the release Mac, view the existing public key
without exporting private material:

```sh
SPARKLE_DIR=$(./scripts/fetch-sparkle.sh)
"$SPARKLE_DIR/bin/generate_keys" --account io.mountainturtle.app -p
```

For a new independent fork, generate your own key by omitting `-p` and replace the
public key and GitHub URLs before distributing the fork. Preserve a secure backup
of your signing Keychain. Do not replace this project's public key on a whim:
existing installations trust it to authenticate updates. The release scripts
refuse to sign with a Keychain key that differs from the committed public key.

For trusted public distribution, use a **Developer ID Application** certificate
and a configured `notarytool` Keychain profile. The scripts require notarization
and verify the stapled app before generating the update archive. See
[Apple's notarization documentation](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution)
and [Sparkle's signing instructions](https://sparkle-project.org/documentation/sandboxing/#code-signing).

An **Apple Development** certificate is sufficient only for an explicitly
labelled preview in this workflow. That preview is not notarized for general
distribution. macOS may require **Privacy & Security → Open Anyway** on first
installation. Ed25519 update authentication does not replace Apple notarization.

## Prepare, review, then publish

1. Increment `VERSION` (three numeric components, for example `0.7.1`), complete
   focused tests and visual app review as appropriate, then commit
   and push the source. Confirm the commit is on `origin/main` and GitHub
   validation passes before publication. Every distributed version must
   increase; never reuse a version or replace published assets.
2. Prepare from a clean checkout using the full certificate display name:

   ```sh
   CODE_SIGN_IDENTITY='Developer ID Application: Your Name (TEAMID)' \
   NOTARY_PROFILE='mountainturtle-notary' \
   ./scripts/prepare-release.sh --notes /path/to/release-notes.md
   ```

   Until Developer ID and notarization are configured, explicitly prepare a preview:

   ```sh
   CODE_SIGN_IDENTITY='Apple Development: Your Name (TEAMID)' \
   ./scripts/prepare-release.sh --preview --notes /path/to/release-notes.md
   ```

   `--notes` is optional; omitting it uses commit subjects since the previous
   version tag. If an earlier version was tagged but never published, supply
   `--notes` covering all changes since the latest published release.
   Preparation builds all app/helper binaries for Apple silicon and
   Intel, verifies signatures, signs the archive/feed, and creates
   `build/releases/vVERSION/`. It uploads nothing. Preparation refuses dirty
   source, a changed source commit during the build, or an existing output folder.
3. Review the Markdown notes, `release.json`, `appcast.xml`, ZIP, and `SHA256SUMS`.
   Test the app's update check and an actual update/relaunch. Confirm the archive
   contains only `Mountain Turtle.app`, that the app can launch from Applications,
   and that required Python/rclone/AWS dependencies are clear to the recipient.
4. Create and push the matching tag at the same prepared commit:

   ```sh
   git tag -a "v$(cat VERSION)" -m "Mountain Turtle $(cat VERSION)"
   git push origin HEAD
   git push origin "v$(cat VERSION)"
   ```

5. Publish the reviewed assets (use `--preview` only for a prepared preview):

   ```sh
   ./scripts/publish-release.sh "build/releases/v$(cat VERSION)"
   ```

   Publication requires the clean prepared commit and exact already-pushed tag.
   It checks every asset checksum and Sparkle signature, creates a draft, uploads
   assets, downloads them again to compare every byte, then publishes the complete
   release as GitHub's latest release. It refuses an existing draft or published
   release; it never uses `--clobber` or moves a tag. If interrupted after creating
   a draft, inspect its assets before explicitly recovering it. A published
   release needs a new version for corrections.

   After publication, the script downloads the public `/latest` feed and each
   versioned asset and compares them with the prepared files. This catches a
   release that exists on GitHub but is not actually served to installed apps.
   If GitHub is still propagating the assets, rerun only the delivery check:

   ```sh
   python3 -B scripts/verify-published-release.py "build/releases/v$(cat VERSION)" --attempts 6
   ```

   The delivery check is read-only. It does not recreate the release or replace
   an asset. A successful delivery check still needs an actual update/relaunch
   test on a Mac running an older version.

Preview releases have **Preview** in their title and an explicit notarization
notice. They are ordinary GitHub Releases, rather than GitHub *prereleases*,
because `/releases/latest/download/` excludes prereleases. Publishing a preview
there makes it available to the app's normal update checks. A separate beta
channel would require a separate feed before using GitHub prereleases.

The GitHub workflow runs tests and builds both architectures on source/tag pushes
and pull requests. It has no signing secrets and does not publish an unsigned
update. Release signing and publication currently run on the release Mac as
the required continuation after a validated source push to `main`.

## Local update testing

`scripts/build.sh` defaults to the host architecture and an ad-hoc signature for
development. To test an older build against a prepared newer release, create a
separate signed app with a lower build number:

```sh
CODE_SIGN_IDENTITY='Apple Development: Your Name (TEAMID)' \
BUILD_VERSION=0.6.99 BUILD_DIR="$PWD/build/update-test" \
./scripts/build.sh
```

`BUILD_VERSION` changes `CFBundleVersion` only; release preparation always uses
the committed `VERSION` for both bundle version values. Setting
`TARGET_ARCHS='arm64 x86_64'` builds universal executables. Keep development/test apps separate from the
installed copy and record whether a test used local assets or a published GitHub
release. A successful build or archive signature check does not prove the
installed update/relaunch flow.
