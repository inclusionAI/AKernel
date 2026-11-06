package main

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/containerd/containerd/errdefs"
	"github.com/containerd/containerd/snapshots"
	"github.com/opencontainers/go-digest"
	ocispec "github.com/opencontainers/image-spec/specs-go/v1"
)

func TestUnregisteredLayersCannotFallBackToDownload(t *testing.T) {
	// A nil underlying snapshotter proves rejection precedes ordinary Prepare.
	b := &bridge{allowed: map[string]bool{}}
	_, err := b.Prepare(context.Background(), "layer", "", snapshots.WithLabels(map[string]string{"containerd.io/snapshot.ref": "sha256:abc"}))
	if !errdefs.IsFailedPrecondition(err) {
		t.Fatalf("Prepare()=%v, want failed precondition", err)
	}
}

func TestRuntimePlanAuthorizesNewImmutableRoot(t *testing.T) {
	path := filepath.Join(t.TempDir(), "plan.json")
	write := func(plan Plan) {
		t.Helper()
		data, err := json.Marshal(plan)
		if err != nil {
			t.Fatal(err)
		}
		if err = os.WriteFile(path, data, 0600); err != nil {
			t.Fatal(err)
		}
	}
	write(Plan{Automatic: true})
	b := &bridge{allowed: map[string]bool{}, planPath: path}
	d := digest.FromString("built after sandbox startup")
	root := "/opt/akernel-images/" + d.Encoded()
	if allowed, err := b.rootAllowed(root); err != nil || allowed {
		t.Fatalf("unexpected initial permission: %t %v", allowed, err)
	}
	write(Plan{Automatic: true, Images: []Image{{Reference: "registry.test/poc/main@" + d.String(), Root: root, ChainID: d.String(), Target: ocispec.Descriptor{Digest: d}}}})
	if allowed, err := b.rootAllowed(root); err != nil || !allowed {
		t.Fatalf("new runtime root was not authorized: %t %v", allowed, err)
	}
	if allowed, err := b.rootAllowed("/etc"); err != nil || allowed {
		t.Fatalf("foreign root authorized: %t %v", allowed, err)
	}
}

func TestInvalidRuntimePlanCannotAuthorizeRoot(t *testing.T) {
	path := filepath.Join(t.TempDir(), "plan.json")
	d := digest.FromString("artifact")
	data := `{"images":[{"reference":"registry.test/poc/main@` + d.String() + `","root":"/etc","chain_id":"` + d.String() + `","target":{"digest":"` + d.String() + `"}}]}`
	if err := os.WriteFile(path, []byte(data), 0600); err != nil {
		t.Fatal(err)
	}
	b := &bridge{allowed: map[string]bool{}, planPath: path}
	_, err := b.Prepare(context.Background(), "root", "", snapshots.WithLabels(map[string]string{rootLabel: "/etc"}))
	if !errdefs.IsInvalidArgument(err) || !strings.Contains(err.Error(), "invalid runtime image plan") {
		t.Fatalf("invalid plan: %v", err)
	}
}

func TestUnregisteredRootCannotBeMounted(t *testing.T) {
	b := &bridge{allowed: map[string]bool{}}
	_, err := b.Prepare(context.Background(), "layer", "", snapshots.WithLabels(map[string]string{rootLabel: "/etc"}))
	if !errdefs.IsInvalidArgument(err) {
		t.Fatalf("Prepare()=%v, want invalid argument", err)
	}
}
