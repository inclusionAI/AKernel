package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/compose-spec/compose-go/v2/types"
)

func TestDockerfileImageDiscovery(t *testing.T) {
	version := "bookworm"
	build := &types.BuildConfig{Args: types.MappingWithEquals{"VERSION": &version}}
	images, err := dockerfileImages(`ARG VERSION=alpine
ARG BASE=debian:${VERSION}-slim
FROM --platform=$TARGETPLATFORM ${BASE} AS build
RUN <<EOF
echo "FROM fake:image"
EOF
FROM build AS other
FROM scratch
COPY --from=build /output /app
COPY --from=busybox:1.37 /bin/busybox /bin/busybox
`, build)
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"docker.io/library/debian:bookworm-slim", "docker.io/library/busybox:1.37"}
	if !reflect.DeepEqual(images, want) {
		t.Fatalf("images=%v, want %v", images, want)
	}
}

func TestRejectsForeignBuildPlatform(t *testing.T) {
	_, err := dockerfileImages("FROM --platform=linux/arm64 ubuntu:24.04\n", &types.BuildConfig{})
	if err == nil {
		t.Fatal("foreign-platform mounts must be rejected before sandbox creation")
	}
}

func TestComposeMixedBuildAndDigestServiceDiscovery(t *testing.T) {
	dir := t.TempDir()
	image := "harborframework/terminal-bench:loadgen@sha256:" + strings.Repeat("a", 64)
	files := map[string]string{
		"Dockerfile":   "FROM ubuntu:24.04\n",
		"compose.yaml": "services:\n  main:\n    build: .\n  loadgen:\n    image: " + image + "\n",
	}
	for name, source := range files {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(source), 0600); err != nil {
			t.Fatal(err)
		}
	}
	input, _ := json.Marshal(map[string]any{"directory": dir, "files": []string{filepath.Join(dir, "compose.yaml")}})
	var output bytes.Buffer
	if err := discover(bytes.NewReader(input), &output); err != nil {
		t.Fatal(err)
	}
	var result struct{ References []string }
	if err := json.Unmarshal(output.Bytes(), &result); err != nil {
		t.Fatal(err)
	}
	want := []string{"docker.io/" + image, "docker.io/library/ubuntu:24.04"}
	if !reflect.DeepEqual(result.References, want) {
		t.Fatalf("got %v, want %v", result.References, want)
	}
}

func TestDiscoveryRejectsClientInputsOutsideFrozenProject(t *testing.T) {
	for _, defect := range []string{"context", "dockerfile", "symlink", "dotenv", "include", "extends"} {
		t.Run(defect, func(t *testing.T) {
			parent := t.TempDir()
			root := filepath.Join(parent, "project")
			if err := os.Mkdir(root, 0700); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(parent, "Dockerfile"), []byte("FROM private.example/secret:client-only\n"), 0600); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(root, "Dockerfile"), []byte("FROM ubuntu:24.04\n"), 0600); err != nil {
				t.Fatal(err)
			}
			compose := "services: {main: {build: .}}\n"
			switch defect {
			case "context":
				compose = "services: {main: {build: ..}}\n"
			case "dockerfile":
				compose = "services: {main: {build: {context: ., dockerfile: ../Dockerfile}}}\n"
			case "symlink":
				if err := os.Remove(filepath.Join(root, "Dockerfile")); err != nil {
					t.Fatal(err)
				}
				if err := os.Symlink(filepath.Join(parent, "Dockerfile"), filepath.Join(root, "Dockerfile")); err != nil {
					t.Fatal(err)
				}
			case "dotenv":
				if err := os.Symlink(filepath.Join(parent, "Dockerfile"), filepath.Join(root, ".env")); err != nil {
					t.Fatal(err)
				}
			case "include":
				compose = "include: ../private.yaml\nservices: {main: {build: .}}\n"
			case "extends":
				compose = "services: {main: {extends: {file: ../private.yaml, service: x}, build: .}}\n"
			}
			file := filepath.Join(root, "compose.yaml")
			if err := os.WriteFile(file, []byte(compose), 0600); err != nil {
				t.Fatal(err)
			}
			input, _ := json.Marshal(map[string]any{"directory": root, "files": []string{file}})
			var output bytes.Buffer
			if err := discover(bytes.NewReader(input), &output); err == nil {
				t.Fatal("outside/unsupported client input was accepted")
			}
			if strings.Contains(output.String(), "private.example") {
				t.Fatal("private client input was disclosed")
			}
		})
	}
}

func TestUnprovidedBuildArgumentRetainsDockerfileDefault(t *testing.T) {
	images, err := dockerfileImages("ARG BASE=ubuntu:24.04\nFROM $BASE\n", &types.BuildConfig{Args: types.MappingWithEquals{"BASE": nil}})
	if err != nil || !reflect.DeepEqual(images, []string{"docker.io/library/ubuntu:24.04"}) {
		t.Fatalf("images=%v err=%v", images, err)
	}
}

func TestComposeMergeAndSeparateVerifierDiscovery(t *testing.T) {
	dir := t.TempDir()
	if err := os.Mkdir(filepath.Join(dir, "loadgen"), 0700); err != nil {
		t.Fatal(err)
	}
	files := map[string]string{
		"Dockerfile":         "FROM ubuntu:24.04\n",
		"loadgen/Dockerfile": "FROM rust:1-slim-bookworm AS build\nFROM debian:bookworm-slim\nCOPY --from=build /x /x\n",
		"base.json":          `{"services":{"main":{"build":{"context":"."}}}}`,
		"compose.yaml":       "services:\n  loadgen:\n    build: ./loadgen\n",
		"verifier.json":      `{"services":{"main":{"build":{"context":".","dockerfile_inline":"FROM python:3.12-slim\n"}}}}`,
	}
	for name, content := range files {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0600); err != nil {
			t.Fatal(err)
		}
	}
	for _, tc := range []struct {
		files []string
		want  []string
	}{
		{[]string{"base.json", "compose.yaml"}, []string{"docker.io/library/debian:bookworm-slim", "docker.io/library/rust:1-slim-bookworm", "docker.io/library/ubuntu:24.04"}},
		{[]string{"verifier.json"}, []string{"docker.io/library/python:3.12-slim"}},
	} {
		paths := []string{}
		for _, name := range tc.files {
			paths = append(paths, filepath.Join(dir, name))
		}
		input, _ := json.Marshal(map[string]any{"directory": dir, "files": paths})
		var output bytes.Buffer
		if err := discover(bytes.NewReader(input), &output); err != nil {
			t.Fatal(err)
		}
		var result struct{ References []string }
		if err := json.Unmarshal(output.Bytes(), &result); err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(result.References, tc.want) {
			t.Fatalf("got %v, want %v", result.References, tc.want)
		}
	}
}
