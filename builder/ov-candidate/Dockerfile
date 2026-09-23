FROM akernel-bm1/all-in-one:canon-8c53bdd-rebuild1
COPY function-proxy /opt/akernel-scheduler/function-proxy
COPY function-master /opt/akernel-scheduler/function-master
COPY runtime-manager /opt/akernel-scheduler/runtime-manager
COPY function-agent /opt/akernel-scheduler/function-agent
COPY config/wrapper-function-proxy /tmp/wrappers/function_proxy
COPY config/wrapper-function-master /tmp/wrappers/function_master
COPY config/wrapper-runtime-manager /tmp/wrappers/runtime_manager
COPY config/wrapper-function-agent /tmp/wrappers/function_agent
RUN set -e  && for n in function_proxy function_master runtime_manager function_agent; do \
      target=/home/yuanrong/functionsystem/bin/$n; \
      if [ -e "$target" ] && [ ! -e "$target.wheel" ]; then mv "$target" "$target.wheel"; fi; \
      install -m 0755 "/tmp/wrappers/$n" "$target"; \
    done  && rm -rf /tmp/wrappers
COPY scripts/install.sh /home/yuanrong/functionsystem/deploy/install.sh
COPY config/elasticity.env /home/yuanrong/functionsystem/deploy/elasticity.env
COPY sandboxd/sandboxd /usr/local/bin/sandboxd
COPY sandboxd/sandbox-logger /usr/local/bin/sandbox-logger
