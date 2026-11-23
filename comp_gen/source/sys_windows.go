package main

import (
	"os/exec"
	"syscall"
)

// hide keeps ffmpeg from flashing a console window on Windows.
func hide(c *exec.Cmd) {
	c.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000}
}
