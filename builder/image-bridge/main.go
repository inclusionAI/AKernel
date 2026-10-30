// image-bridge connects sandboxd-mounted immutable images to Docker/containerd.
// Only OCI metadata is registered locally; image filesystem data stays mounted.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"

	containerd "github.com/containerd/containerd"
	snapshotsapi "github.com/containerd/containerd/api/services/snapshots/v1"
	"github.com/containerd/containerd/content"
	"github.com/containerd/containerd/contrib/snapshotservice"
	"github.com/containerd/containerd/errdefs"
	"github.com/containerd/containerd/images"
	"github.com/containerd/containerd/mount"
	"github.com/containerd/containerd/namespaces"
	"github.com/containerd/containerd/snapshots"
	"github.com/containerd/containerd/snapshots/overlay"
	"github.com/opencontainers/go-digest"
	ocispec "github.com/opencontainers/image-spec/specs-go/v1"
	"golang.org/x/sys/unix"
	"google.golang.org/grpc"
)

const rootLabel = "containerd.io/snapshot/agent-service.image-root"

type Blob struct {
	Descriptor ocispec.Descriptor `json:"descriptor"`
	File       string             `json:"file"`
}
type Image struct {
	Reference string             `json:"reference"`
	Root      string             `json:"root"`
	ChainID   string             `json:"chain_id"`
	Target    ocispec.Descriptor `json:"target"`
	Config    ocispec.Descriptor `json:"config"`
	Blobs     []Blob             `json:"blobs"`
}
type Plan struct {
	Images    []Image `json:"images"`
	Automatic bool    `json:"automatic,omitempty"`
}
type Seed struct {
	Source string `json:"source"`
	Target string `json:"target"`
}

type bridge struct {
	snapshots.Snapshotter
	mu      sync.Mutex
	allowed map[string]bool
	seeds   []Seed
	state   string
}

func (b *bridge) Prepare(ctx context.Context, key, parent string, opts ...snapshots.Opt) ([]mount.Mount, error) {
	info := snapshots.Info{}
	for _, opt := range opts {
		if err := opt(&info); err != nil {
			return nil, err
		}
	}
	source := info.Labels[rootLabel]
	if source == "" {
		if info.Labels["containerd.io/snapshot.ref"] != "" {
			return nil, fmt.Errorf("image layer is not registered by the sandboxd mount plan: %w", errdefs.ErrFailedPrecondition)
		}
		return b.Snapshotter.Prepare(ctx, key, parent, opts...)
	}
	if !b.allowed[source] || parent != "" {
		return nil, fmt.Errorf("unregistered image root or nonempty parent: %w", errdefs.ErrInvalidArgument)
	}
	var stat unix.Statfs_t
	if err := unix.Statfs(source, &stat); err != nil {
		return nil, err
	}
	if stat.Flags&unix.ST_RDONLY == 0 {
		return nil, fmt.Errorf("image root must be read-only: %s", source)
	}
	mounts, err := b.Snapshotter.Prepare(ctx, key, parent, opts...)
	if err != nil {
		return nil, err
	}
	if len(mounts) != 1 || mounts[0].Type != "bind" {
		return nil, fmt.Errorf("unexpected root snapshot mounts: %+v", mounts)
	}
	target := mounts[0].Source
	if err := bindReadOnly(source, target); err != nil {
		return nil, err
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	b.seeds = append(b.seeds, Seed{source, target})
	data, err := json.Marshal(b.seeds)
	if err != nil {
		return nil, err
	}
	if err = os.WriteFile(b.state+".new", data, 0600); err != nil {
		return nil, err
	}
	if err = os.Rename(b.state+".new", b.state); err != nil {
		return nil, err
	}
	log.Printf("seed mounted: key=%s source=%s target=%s", key, source, target)
	return mounts, nil
}

func bindReadOnly(source, target string) error {
	if err := unix.Mount(source, target, "", unix.MS_BIND|unix.MS_REC, ""); err != nil {
		return err
	}
	if err := unix.Mount("", target, "", unix.MS_BIND|unix.MS_REMOUNT|unix.MS_RDONLY, ""); err != nil {
		return err
	}
	return nil
}

func (b *bridge) Remove(ctx context.Context, key string) error {
	info, err := b.Stat(ctx, key)
	if err != nil {
		return err
	}
	if info.Labels[rootLabel] != "" {
		// Pinned mounts live until the outer Sandbox is deleted. Image GC must not
		// detach a shared root behind a running container or a saved checkpoint.
		return fmt.Errorf("mounted image is pinned to this Sandbox: %w", errdefs.ErrFailedPrecondition)
	}
	return b.Snapshotter.Remove(ctx, key)
}

func readPlan(path string) (Plan, error) {
	var plan Plan
	data, err := os.ReadFile(path)
	if err != nil {
		return plan, err
	}
	err = json.Unmarshal(data, &plan)
	if err != nil {
		return plan, err
	}
	if len(plan.Images) == 0 && !plan.Automatic {
		return plan, errors.New("empty image plan")
	}
	for _, image := range plan.Images {
		if !strings.Contains(image.Reference, "@sha256:") || !filepath.IsAbs(image.Root) {
			return plan, errors.New("images require immutable references and absolute mount paths")
		}
		if _, err := digest.Parse(image.ChainID); err != nil {
			return plan, err
		}
		suffix := strings.SplitN(image.Reference, "@", 2)[1]
		if suffix != image.Target.Digest.String() || image.Root != "/opt/akernel-images/"+image.Target.Digest.Encoded() {
			return plan, errors.New("image digest, target and mount path disagree")
		}
	}
	return plan, nil
}

func serve(plan Plan, root, socket string) error {
	sn, err := overlay.NewSnapshotter(root)
	if err != nil {
		return err
	}
	defer sn.Close()
	b := &bridge{Snapshotter: sn, allowed: map[string]bool{}, state: filepath.Join(root, "mounted-seeds.json")}
	for _, image := range plan.Images {
		b.allowed[image.Root] = true
	}
	if data, err := os.ReadFile(b.state); err == nil {
		if err = json.Unmarshal(data, &b.seeds); err != nil {
			return err
		}
		for _, seed := range b.seeds {
			if !b.allowed[seed.Source] {
				return errors.New("saved seed not in current image plan")
			}
			// Existing mounts survive Sandbox reload. On a daemon-only restart they
			// also remain present; compare mountinfo before adding a bind.
			data, err := os.ReadFile("/proc/self/mountinfo")
			if err != nil {
				return err
			}
			mounted := false
			for _, line := range strings.Split(string(data), "\n") {
				fields := strings.Fields(line)
				if len(fields) > 4 && fields[4] == seed.Target {
					mounted = true
					break
				}
			}
			if !mounted {
				if err = bindReadOnly(seed.Source, seed.Target); err != nil {
					return err
				}
			}
		}
	} else if !os.IsNotExist(err) {
		return err
	}
	if err = os.MkdirAll(filepath.Dir(socket), 0700); err != nil {
		return err
	}
	listener, err := net.Listen("unix", socket)
	if err != nil {
		return err
	}
	server := grpc.NewServer()
	snapshotsapi.RegisterSnapshotsServer(server, snapshotservice.FromSnapshotter(b))
	log.Printf("image bridge snapshotter ready: %s", socket)
	return server.Serve(listener)
}

func register(plan Plan, planDir, address string) error {
	client, err := containerd.New(address)
	if err != nil {
		return err
	}
	defer client.Close()
	ctx := namespaces.WithNamespace(context.Background(), "moby")
	ctx, done, err := client.WithLease(ctx)
	if err != nil {
		return err
	}
	defer done(ctx)
	cs := client.ContentStore()
	for _, image := range plan.Images {
		for _, blob := range image.Blobs {
			if filepath.Base(blob.File) != blob.File {
				return errors.New("invalid metadata file name")
			}
			data, err := os.ReadFile(filepath.Join(planDir, blob.File))
			if err != nil {
				return err
			}
			labels := map[string]string{}
			var object struct {
				Config    *ocispec.Descriptor  `json:"config"`
				Manifests []ocispec.Descriptor `json:"manifests"`
			}
			if err = json.Unmarshal(data, &object); err != nil {
				return err
			}
			if object.Config != nil {
				labels["containerd.io/gc.ref.content.config"] = object.Config.Digest.String()
			}
			for n, d := range object.Manifests {
				labels[fmt.Sprintf("containerd.io/gc.ref.content.m.%d", n)] = d.Digest.String()
			}
			if blob.Descriptor.Digest == image.Config.Digest {
				labels["containerd.io/gc.ref.snapshot.akernel"] = image.ChainID
			}
			if err = content.WriteBlob(ctx, cs, blob.Descriptor.Digest.String(), bytes.NewReader(data), blob.Descriptor, content.WithLabels(labels)); err != nil {
				return err
			}
		}
		sn := client.SnapshotService("akernel")
		if _, err = sn.Stat(ctx, image.ChainID); errdefs.IsNotFound(err) {
			key := "seed-" + strings.TrimPrefix(image.ChainID, "sha256:")
			if _, err = sn.Prepare(ctx, key, "", snapshots.WithLabels(map[string]string{rootLabel: image.Root})); err != nil {
				return err
			}
			if err = sn.Commit(ctx, image.ChainID, key, snapshots.WithLabels(map[string]string{rootLabel: image.Root})); err != nil {
				return err
			}
		} else if err != nil {
			return err
		}
		existing, err := client.ImageService().Get(ctx, image.Reference)
		if errdefs.IsNotFound(err) {
			_, err = client.ImageService().Create(ctx, images.Image{Name: image.Reference, Target: image.Target})
		} else if err == nil && existing.Target.Digest != image.Target.Digest {
			return errors.New("image reference changed")
		}
		if err != nil {
			return err
		}
		log.Printf("registered mounted image without layer transfer: %s chain=%s", image.Reference, image.ChainID)
	}
	return nil
}

func main() {
	mode := flag.String("mode", "serve", "serve, register or discover")
	planPath := flag.String("plan", "", "mounted image plan JSON")
	root := flag.String("root", "/var/lib/docker/akernel-snapshotter", "snapshot metadata and writable layers")
	socket := flag.String("socket", "/run/akernel-image-bridge/snapshotter.sock", "snapshotter socket")
	address := flag.String("containerd", "/run/akernel-image-bridge/containerd.sock", "containerd socket")
	flag.Parse()
	if *mode == "capabilities" {
		returnErr := json.NewEncoder(os.Stdout).Encode(map[string]any{"discover_schema": 2})
		if returnErr != nil {
			log.Fatal(returnErr)
		}
		return
	}
	if *mode == "discover" {
		if err := discover(os.Stdin, os.Stdout); err != nil {
			log.Fatal(err)
		}
		return
	}
	plan, err := readPlan(*planPath)
	if err != nil {
		log.Fatal(err)
	}
	if *mode == "serve" {
		err = serve(plan, *root, *socket)
	} else if *mode == "register" {
		err = register(plan, filepath.Dir(*planPath), *address)
	} else {
		err = errors.New("unknown mode")
	}
	if err != nil {
		log.Fatal(err)
	}
}
