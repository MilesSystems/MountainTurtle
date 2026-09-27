package main

import (
	"context"
	"encoding/json"
	"github.com/rclone/rclone/cmd"
	"github.com/rclone/rclone/fs"
	"github.com/rclone/rclone/fs/list"
	"github.com/spf13/cobra"
	"os"
	"path"
)

type treeEntry struct {
	Name      string  `json:"name"`
	Directory bool    `json:"directory"`
	Size      int64   `json:"size"`
	Modified  float64 `json:"modified"`
}

type treePage struct {
	Entries  []treeEntry `json:"entries,omitempty"`
	Complete bool        `json:"complete"`
	Error    string      `json:"error,omitempty"`
}

func init() {
	cmd.Root.AddCommand(&cobra.Command{
		Use: "tree-list remote:path", Short: "Stream one directory for Mountain Turtle's outline browser.",
		Run: func(command *cobra.Command, args []string) {
			cmd.CheckArgs(1, 1, command, args)
			f := cmd.NewFsDir(args)
			cmd.Run(false, false, command, func() error {
				ctx := context.Background()
				encoder := json.NewEncoder(os.Stdout)
				err := list.DirPages(ctx, f, "", func(entries fs.DirEntries) error {
					page := treePage{}
					flush := func() error {
						if len(page.Entries) == 0 {
							return nil
						}
						err := encoder.Encode(page)
						page.Entries = nil
						return err
					}
					for _, e := range entries {
						_, directory := e.(fs.Directory)
						page.Entries = append(page.Entries, treeEntry{path.Base(e.Remote()), directory, e.Size(), float64(e.ModTime(ctx).Unix())})
						if len(page.Entries) >= 256 {
							if err := flush(); err != nil {
								return err
							}
						}
					}
					return flush()
				})
				if err != nil {
					_ = encoder.Encode(treePage{Error: "The folder listing could not finish. Retry, or check the connection."})
					return err
				}
				return encoder.Encode(treePage{Complete: true})
			})
		},
	})
}
