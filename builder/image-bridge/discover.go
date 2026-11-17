package main

// Discovery uses the Compose and Dockerfile implementations, not regular
// expressions over user files. The original project is still built by Docker.
import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"

	"github.com/compose-spec/compose-go/v2/cli"
	"github.com/compose-spec/compose-go/v2/types"
	"github.com/distribution/reference"
	"github.com/moby/buildkit/frontend/dockerfile/instructions"
	"github.com/moby/buildkit/frontend/dockerfile/parser"
	"github.com/moby/buildkit/frontend/dockerfile/shell"
	"gopkg.in/yaml.v3"
)

func projectPath(root, path string) (string, error) {
	if !filepath.IsAbs(path) {
		path = filepath.Join(root, path)
	}
	resolved, err := filepath.EvalSymlinks(path)
	if err != nil {
		return "", fmt.Errorf("cannot resolve managed project input: %w", err)
	}
	relative, err := filepath.Rel(root, resolved)
	if err != nil || relative == ".." || strings.HasPrefix(relative, ".."+string(filepath.Separator)) {
		return "", fmt.Errorf("managed Compose inputs must stay within the project directory")
	}
	return resolved, nil
}

func validateComposeSources(root string, files []string) error {
	// Only explicitly supplied top-level files may live outside the frozen
	// context (SDK overlays do). Never let user YAML load other client files.
	for _, file := range files {
		data, err := os.ReadFile(file)
		if err != nil {
			return err
		}
		var document map[string]any
		if err = yaml.Unmarshal(data, &document); err != nil {
			return err
		}
		if document["include"] != nil {
			return fmt.Errorf("managed Compose discovery does not support include; supply explicit project files")
		}
		services, _ := document["services"].(map[string]any)
		for _, value := range services {
			service, _ := value.(map[string]any)
			extends, _ := service["extends"].(map[string]any)
			if extends["file"] != nil {
				return fmt.Errorf("managed Compose discovery does not support extends.file; supply explicit project files")
			}
		}
	}
	if _, err := os.Lstat(filepath.Join(root, ".env")); err == nil {
		_, err = projectPath(root, ".env")
		return err
	} else if !os.IsNotExist(err) {
		return err
	}
	return nil
}

func dockerfileImages(source string, build *types.BuildConfig) ([]string, error) {
	parsed, err := parser.Parse(strings.NewReader(source))
	if err != nil {
		return nil, err
	}
	stages, args, err := instructions.Parse(parsed.AST, nil)
	if err != nil {
		return nil, err
	}
	values := map[string]string{
		"BUILDPLATFORM": "linux/amd64", "BUILDOS": "linux", "BUILDARCH": "amd64", "BUILDVARIANT": "",
		"TARGETPLATFORM": "linux/amd64", "TARGETOS": "linux", "TARGETARCH": "amd64", "TARGETVARIANT": "",
	}
	lex := shell.NewLex(parsed.EscapeToken)
	expand := func(word string) (string, error) {
		env := make([]string, 0, len(values))
		for k, v := range values {
			env = append(env, k+"="+v)
		}
		value, _, err := lex.ProcessWord(word, shell.EnvsFromSlice(env))
		return value, err
	}
	for _, command := range args {
		for _, arg := range command.Args {
			if override, ok := build.Args[arg.Key]; ok && override != nil {
				values[arg.Key] = *override
				continue
			} else if ok {
				if value, present := os.LookupEnv(arg.Key); present {
					values[arg.Key] = value
					continue
				}
			}
			if arg.Value != nil {
				value, err := expand(*arg.Value)
				if err != nil {
					return nil, err
				}
				values[arg.Key] = value
			}
		}
	}
	var images []string
	knownStages := map[string]bool{}
	for _, stage := range stages {
		knownStages[strings.ToLower(stage.Name)] = true
	}
	add := func(name string, allowStage bool) error {
		if strings.EqualFold(name, "scratch") || (allowStage && knownStages[strings.ToLower(name)]) {
			return nil
		}
		if allowStage {
			if _, err := strconv.Atoi(name); err == nil {
				return nil
			}
		}
		if context, ok := build.AdditionalContexts[name]; ok {
			if !strings.HasPrefix(context, "docker-image://") {
				return fmt.Errorf("automatic image preparation needs a docker-image context for %s", name)
			}
			name = strings.TrimPrefix(context, "docker-image://")
		}
		ref, err := reference.ParseNormalizedNamed(name)
		if err != nil {
			return fmt.Errorf("invalid external image %q: %w", name, err)
		}
		images = append(images, reference.TagNameOnly(ref).String())
		return nil
	}
	previousStages := map[string]bool{}
	for _, stage := range stages {
		if stage.Platform != "" {
			platform, err := expand(stage.Platform)
			if err != nil || platform != "linux/amd64" {
				return nil, fmt.Errorf("automatic image preparation supports linux/amd64, got %q", platform)
			}
		}
		name, err := expand(stage.BaseName)
		if err != nil {
			return nil, err
		}
		if !previousStages[strings.ToLower(name)] {
			if err = add(name, false); err != nil {
				return nil, err
			}
		}
		previousStages[strings.ToLower(stage.Name)] = true
		for _, command := range stage.Commands {
			if copy, ok := command.(*instructions.CopyCommand); ok && copy.From != "" {
				if err = add(copy.From, true); err != nil {
					return nil, err
				}
			}
			if run, ok := command.(*instructions.RunCommand); ok {
				for _, mount := range instructions.GetMounts(run) {
					if mount.From != "" {
						if err = add(mount.From, true); err != nil {
							return nil, err
						}
					}
				}
			}
		}
	}
	return images, nil
}

func discover(input io.Reader, output io.Writer) error {
	var request struct {
		Directory string   `json:"directory"`
		Files     []string `json:"files"`
	}
	if err := json.NewDecoder(input).Decode(&request); err != nil {
		return err
	}
	root, err := filepath.Abs(request.Directory)
	if err != nil {
		return err
	}
	root, err = filepath.EvalSymlinks(root)
	if err != nil {
		return err
	}
	if err = validateComposeSources(root, request.Files); err != nil {
		return err
	}
	options, err := cli.NewProjectOptions(request.Files,
		cli.WithWorkingDirectory(request.Directory), cli.WithName("harbor"),
		cli.WithOsEnv, cli.WithEnvFiles(), cli.WithDotEnv,
		cli.WithoutEnvironmentResolution,
	)
	if err != nil {
		return err
	}
	project, err := options.LoadProject(context.Background())
	if err != nil {
		return err
	}
	seen := map[string]bool{}
	for _, service := range project.Services {
		if service.Platform != "" && service.Platform != "linux/amd64" {
			return fmt.Errorf("service %s requires unsupported platform", service.Name)
		}
		if service.Build == nil {
			ref, err := reference.ParseNormalizedNamed(service.Image)
			if err != nil {
				return err
			}
			seen[reference.TagNameOnly(ref).String()] = true
			continue
		}
		build := service.Build
		contextPath, err := projectPath(root, build.Context)
		if err != nil {
			return fmt.Errorf("service %s build context: %w", service.Name, err)
		}
		build.Context = contextPath
		build.Args = build.Args.Resolve(func(key string) (string, bool) {
			value, ok := project.Environment[key]
			return value, ok
		})
		for _, platform := range build.Platforms {
			if platform != "linux/amd64" {
				return fmt.Errorf("service %s requires unsupported build platform", service.Name)
			}
		}
		source := build.DockerfileInline
		if source == "" {
			name := build.Dockerfile
			if name == "" {
				name = "Dockerfile"
			}
			if !filepath.IsAbs(name) {
				name = filepath.Join(build.Context, name)
			}
			name, err = projectPath(root, name)
			if err != nil {
				return fmt.Errorf("service %s Dockerfile: %w", service.Name, err)
			}
			data, err := os.ReadFile(name)
			if err != nil {
				return fmt.Errorf("read Dockerfile for service %s: %w", service.Name, err)
			}
			source = string(data)
		}
		images, err := dockerfileImages(source, build)
		if err != nil {
			return fmt.Errorf("discover bases for service %s: %w", service.Name, err)
		}
		for _, image := range images {
			seen[image] = true
		}
	}
	images := make([]string, 0, len(seen))
	for image := range seen {
		images = append(images, image)
	}
	sort.Strings(images)
	return json.NewEncoder(output).Encode(map[string]any{"references": images})
}
