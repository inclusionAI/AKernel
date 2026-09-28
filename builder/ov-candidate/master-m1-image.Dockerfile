FROM akernel-bm1/all-in-one@sha256:d307f76990fdff27770904b9aef6d2cdf8358dfd26828b2ebbb03aca321758b0
# mode 0755 comes from the context file (chmod before build): legacy
# builder has no --chmod; plain COPY preserves the context file mode and
# still yields exactly ONE new layer containing exactly this one file.
COPY function_master /opt/akernel-scheduler/function-master/function_master
