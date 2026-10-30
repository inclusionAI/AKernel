package main

import (
	"context"
	"testing"

	"github.com/containerd/containerd/errdefs"
	"github.com/containerd/containerd/snapshots"
)

func TestUnregisteredLayersCannotFallBackToDownload(t *testing.T) {
	// A nil underlying snapshotter proves rejection precedes ordinary Prepare.
	b := &bridge{allowed: map[string]bool{}}
	_, err := b.Prepare(context.Background(), "layer", "", snapshots.WithLabels(map[string]string{"containerd.io/snapshot.ref": "sha256:abc"}))
	if !errdefs.IsFailedPrecondition(err) {
		t.Fatalf("Prepare()=%v, want failed precondition", err)
	}
}

func TestUnregisteredRootCannotBeMounted(t *testing.T) {
	b := &bridge{allowed: map[string]bool{}}
	_, err := b.Prepare(context.Background(), "layer", "", snapshots.WithLabels(map[string]string{rootLabel: "/etc"}))
	if !errdefs.IsInvalidArgument(err) {
		t.Fatalf("Prepare()=%v, want invalid argument", err)
	}
}
