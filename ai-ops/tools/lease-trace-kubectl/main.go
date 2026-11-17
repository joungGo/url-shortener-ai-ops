package main

import (
	"k8s.io/component-base/cli"
	"k8s.io/kubectl/pkg/cmd"
	"os"
)

func main() { os.Exit(cli.Run(cmd.NewDefaultKubectlCommand())) }
