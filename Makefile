# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0

SHELL := /usr/bin/bash

VENDOR ?= aliyun
ENV ?= default
IMAGE_TAG ?=
IMAGE_REPOSITORY ?=
GVISOR_RELEASE ?=
GVISOR_RELEASE_BASE_URL ?=
OPEN_YR_CORE_WHEEL_URL ?= https://openyuanrong.obs.cn-southwest-2.myhuaweicloud.com/daily/20260822051203/linux/amd64/openyuanrong_core-0.7.0%2B87cba622b491-py3-none-manylinux_2_31_x86_64.whl
OPEN_YR_CORE_WHEEL_SHA256 ?= 9eb44e1ea59153ab9a65a81fc32450c09376e835732290046d028cec2db3b200
OPEN_YR_RRT_WHEEL_URL ?= https://openyuanrong.obs.cn-southwest-2.myhuaweicloud.com/daily/20260822042459/linux/amd64/openyuanrong_rrt-0.7.0%2B87cba622b491-py3-none-manylinux_2_31_x86_64.whl
OPEN_YR_RRT_WHEEL_SHA256 ?= 3aff1b4a676ca28992a2478adab900bc7bd1e76928cc12016ae50fea412a68c4
TOKEN_TTL ?= $(if $(TTL),$(TTL),24h)
TENANT ?= default
ROLE ?= developer
FORCE ?= 0
NON_INTERACTIVE ?= 0
REGION ?=
CLUSTER_NAME ?=
ZONE_IDS ?=
AVAILABILITY_ZONE ?=
VSWITCH_CIDRS ?=
NODE_POOL_SIZE ?=
NODE_POOL_INSTANCE_TYPES ?=
NODE_FLAVOR_ID ?=
NODE_POOL_KEY_NAME ?=
NODE_POOL_LOGIN_PASSWORD ?=
ACR_NAMESPACE ?=
MONITOR_STORAGE_CLASS ?=
INSTALL_MONITOR ?=
INSTALL_DRAGONFLY ?=
ENABLE_RUNC ?=
GRAFANA_PUBLIC_ACCESS ?=
GRAFANA_ADMIN_PASSWORD ?=
IAM_SEED_HEX ?=

.PHONY: help
help:
	@echo "AKernel helper targets"
	@echo
	@echo "  VENDOR defaults to aliyun."
	@echo
	@echo "  make check                         Check required local tools"
	@echo "  make config                        Create .akernel/default config"
	@echo "  make config ENV=<name>             Create or update a named config"
	@echo "  make config FORCE=1                Overwrite an existing config without prompting"
	@echo "  make config NON_INTERACTIVE=1 ...  Generate config from Make variables"
	@echo "  make config INSTALL_DRAGONFLY=true Enable optional P2P image distribution"
	@echo "  make config ENABLE_RUNC=true       Build and register the optional runc runtime"
	@echo "  make build IMAGE_TAG=<tag>          Build the all-in-one image"
	@echo "  make build RUNTIME_PROFILE=python   Include optional Python runtimes"
	@echo "  make build AKERNEL_ENABLE_KATA=false Exclude the optional Kata payload"
	@echo "  make build AKERNEL_ENABLE_RUNC=true Include the optional runc payload"
	@echo "  make build GVISOR_RELEASE=<tag>     Override the pinned official gVisor tag"
	@echo "  make versions                       Show locally selected component versions"
	@echo "  make push                          Push the configured all-in-one image"
	@echo "  make plan                          Terraform plan"
	@echo "  make deploy                        Terraform apply"
	@echo "  make token TTL=24h                 Generate a local JWT token"
	@echo "  make print-env                     Print SDK environment exports"
	@echo "  make sdk-check                     Lint, type-check, and test the Python SDK"
	@echo "  make deploy-script-check           Check deployment script syntax"
	@echo "  make e2e                           Run the basic SDK e2e example"
	@echo "  make destroy                       Destroy cloud resources"

.PHONY: check
check:
	@./deploy/scripts/check-prereqs.sh --vendor "$(VENDOR)"

.PHONY: config
config:
	@args=(--vendor "$(VENDOR)" --env "$(ENV)"); \
	if [[ "$(FORCE)" == "1" ]]; then args+=(--force); fi; \
	if [[ "$(NON_INTERACTIVE)" == "1" ]]; then args+=(--non-interactive); fi; \
	if [[ -n "$(REGION)" ]]; then args+=(--region "$(REGION)"); fi; \
	if [[ -n "$(CLUSTER_NAME)" ]]; then args+=(--cluster-name "$(CLUSTER_NAME)"); fi; \
	if [[ -n "$(ZONE_IDS)" ]]; then args+=(--zone-ids "$(ZONE_IDS)"); fi; \
	if [[ -n "$(AVAILABILITY_ZONE)" ]]; then args+=(--availability-zone "$(AVAILABILITY_ZONE)"); fi; \
	if [[ -n "$(VSWITCH_CIDRS)" ]]; then args+=(--vswitch-cidrs "$(VSWITCH_CIDRS)"); fi; \
	if [[ -n "$(NODE_POOL_SIZE)" ]]; then args+=(--node-pool-size "$(NODE_POOL_SIZE)"); fi; \
	if [[ -n "$(NODE_POOL_INSTANCE_TYPES)" ]]; then args+=(--node-pool-instance-types "$(NODE_POOL_INSTANCE_TYPES)"); fi; \
	if [[ -n "$(NODE_FLAVOR_ID)" ]]; then args+=(--node-flavor-id "$(NODE_FLAVOR_ID)"); fi; \
	if [[ -n "$(NODE_POOL_KEY_NAME)" ]]; then args+=(--node-pool-key-name "$(NODE_POOL_KEY_NAME)"); fi; \
	if [[ -n "$(NODE_POOL_LOGIN_PASSWORD)" ]]; then args+=(--node-pool-login-password "$(NODE_POOL_LOGIN_PASSWORD)"); fi; \
	if [[ -n "$(ACR_NAMESPACE)" ]]; then args+=(--acr-namespace "$(ACR_NAMESPACE)"); fi; \
	if [[ -n "$(MONITOR_STORAGE_CLASS)" ]]; then args+=(--monitor-storage-class "$(MONITOR_STORAGE_CLASS)"); fi; \
	if [[ -n "$(IMAGE_REPOSITORY)" ]]; then args+=(--image-repository "$(IMAGE_REPOSITORY)"); fi; \
	if [[ -n "$(IMAGE_TAG)" ]]; then args+=(--image-tag "$(IMAGE_TAG)"); fi; \
	if [[ -n "$(INSTALL_MONITOR)" ]]; then args+=(--install-monitor "$(INSTALL_MONITOR)"); fi; \
	if [[ -n "$(INSTALL_DRAGONFLY)" ]]; then args+=(--install-dragonfly "$(INSTALL_DRAGONFLY)"); fi; \
	if [[ -n "$(ENABLE_RUNC)" ]]; then args+=(--enable-runc "$(ENABLE_RUNC)"); fi; \
	if [[ -n "$(GRAFANA_PUBLIC_ACCESS)" ]]; then args+=(--grafana-public-access "$(GRAFANA_PUBLIC_ACCESS)"); fi; \
	if [[ -n "$(GRAFANA_ADMIN_PASSWORD)" ]]; then args+=(--grafana-admin-password "$(GRAFANA_ADMIN_PASSWORD)"); fi; \
	if [[ -n "$(IAM_SEED_HEX)" ]]; then args+=(--iam-seed-hex "$(IAM_SEED_HEX)"); fi; \
	./deploy/scripts/configure.sh "$${args[@]}"

.PHONY: build
build:
	@args=(--env "$(ENV)"); \
	if [[ -n "$(IMAGE_REPOSITORY)" ]]; then args+=(--repository "$(IMAGE_REPOSITORY)"); fi; \
	if [[ -n "$(IMAGE_TAG)" ]]; then args+=(--tag "$(IMAGE_TAG)"); fi; \
	if [[ -n "$(RUNTIME_PROFILE)" ]]; then args+=(--runtime-profile "$(RUNTIME_PROFILE)"); fi; \
	if [[ -n "$(GVISOR_RELEASE)" ]]; then args+=(--gvisor-release "$(GVISOR_RELEASE)"); fi; \
	if [[ -n "$(GVISOR_RELEASE_BASE_URL)" ]]; then args+=(--gvisor-release-base-url "$(GVISOR_RELEASE_BASE_URL)"); fi; \
	if [[ -n "$(OPEN_YR_CORE_WHEEL_URL)" ]]; then args+=(--open-yr-core-wheel-url "$(OPEN_YR_CORE_WHEEL_URL)"); fi; \
	if [[ -n "$(OPEN_YR_CORE_WHEEL_SHA256)" ]]; then args+=(--open-yr-core-wheel-sha256 "$(OPEN_YR_CORE_WHEEL_SHA256)"); fi; \
	if [[ -n "$(OPEN_YR_RRT_WHEEL_URL)" ]]; then args+=(--open-yr-rrt-wheel-url "$(OPEN_YR_RRT_WHEEL_URL)"); fi; \
	if [[ -n "$(OPEN_YR_RRT_WHEEL_SHA256)" ]]; then args+=(--open-yr-rrt-wheel-sha256 "$(OPEN_YR_RRT_WHEEL_SHA256)"); fi; \
	./deploy/scripts/build-image.sh "$${args[@]}"

.PHONY: versions
versions:
	@./deploy/scripts/build-image.sh --print-component-versions

.PHONY: push
push:
	@./deploy/scripts/push-image.sh --vendor "$(VENDOR)" --env "$(ENV)"

.PHONY: plan
plan:
	@./deploy/scripts/deploy.sh --vendor "$(VENDOR)" --env "$(ENV)" --plan

.PHONY: deploy
deploy:
	@./deploy/scripts/deploy.sh --vendor "$(VENDOR)" --env "$(ENV)" --apply

.PHONY: token
token:
	@./deploy/scripts/generate-token.py \
		--env "$(ENV)" \
		--tenant "$(TENANT)" \
		--role "$(ROLE)" \
		--ttl "$(TOKEN_TTL)" \
		--print-export \
		--write-file ".akernel/$(ENV)/token"

.PHONY: print-env
print-env:
	@./deploy/scripts/print-sdk-env.sh --vendor "$(VENDOR)" --env "$(ENV)"

.PHONY: e2e
e2e:
	@./deploy/scripts/run-e2e.sh --env "$(ENV)"

.PHONY: sdk-test
sdk-test:
	@PYTHONPATH=sdk/python python3 -m unittest discover \
		-s sdk/python/tests/unit -t sdk/python -v

.PHONY: sdk-check
sdk-check: sdk-test
	@set -e; \
	cd sdk/python; \
	python3 -m ruff check akernel_sdk tests; \
	python3 -m mypy akernel_sdk

.PHONY: deploy-script-check
deploy-script-check:
	@set -euo pipefail; \
	while IFS= read -r -d '' script; do \
		bash -n "$$script"; \
	done < <(git ls-files -z -- 'deploy/**/*.sh'); \
	while IFS= read -r -d '' template; do \
		if ! bash -n <( \
			sed \
				-e '/^[[:space:]]*%{.*}[[:space:]]*$$/d' \
				-e 's/[$$][$$]/$$/g' \
				"$$template" \
		); then \
			echo "Invalid shell template syntax: $$template" >&2; \
			exit 1; \
		fi; \
	done < <(git ls-files -z -- 'deploy/**/*.sh.tftpl'); \
	git ls-files -z -- 'deploy/**/*.py' | \
		xargs -0 -r python3 -c \
		'import pathlib, sys; [compile(pathlib.Path(path).read_bytes(), path, "exec") for path in sys.argv[1:]]'

.PHONY: destroy
destroy:
	@args=(--vendor "$(VENDOR)" --env "$(ENV)"); \
	if [[ "$(AUTO_APPROVE)" == "1" ]]; then args+=(--yes); fi; \
	./deploy/scripts/destroy.sh "$${args[@]}"
