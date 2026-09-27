package main

import (
	_ "github.com/rclone/rclone/backend/local"
	_ "github.com/rclone/rclone/backend/s3"
	_ "github.com/rclone/rclone/backend/sftp"
	_ "github.com/rclone/rclone/backend/union"
	"github.com/rclone/rclone/cmd"
	_ "github.com/rclone/rclone/cmd/nfsmount"
	_ "github.com/rclone/rclone/cmd/serve/nfs"
	_ "github.com/rclone/rclone/cmd/version"
)

func main() { cmd.Main() }
