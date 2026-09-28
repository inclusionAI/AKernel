FROM akernel-bm1/all-in-one@sha256:d307f76990fdff27770904b9aef6d2cdf8358dfd26828b2ebbb03aca321758b0
COPY --chmod=755 function_master /opt/akernel-scheduler/function-master/function_master
