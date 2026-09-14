# Third-party notices

Mountain Turtle's original source code, documentation, and project artwork are
licensed under the MIT license in this repository. The application invokes
separately installed tools and links to Apple system frameworks. This notice
does not relicense those components.

| Component | How it is used | Upstream license or terms |
| --- | --- | --- |
| rclone | Separately installed executable providing S3 access, NFS mounting, and the file cache | [MIT; Nick Craig-Wood and contributors](https://rclone.org/licence/) |
| AWS CLI v2 | Separately installed executable for AWS profiles and SSO sign-in | [Apache License 2.0](https://github.com/aws/aws-cli/blob/v2/LICENSE.txt); its distribution includes additional dependency notices |
| Python 3 | Separately installed interpreter; service uses the standard library | [Python Software Foundation license and associated notices](https://docs.python.org/3/license.html) |
| SwiftUI, AppKit, Finder Sync, Foundation, and macOS tools | Apple-provided system frameworks and tools | Applicable Apple SDK and operating-system terms; these frameworks are not redistributed here |

No rclone, AWS CLI, or Python binary is vendored by the current build. If a future
release bundles dependencies, it must carry the actual licenses and notices for
those versions and their bundled dependencies.

Mountain Duck and Cyberduck are separate products. Their names may be used to
describe the problem space; no affiliation or endorsement is claimed. Mountain
Turtle does not include their proprietary application code, logos, screenshots,
or other artwork.
