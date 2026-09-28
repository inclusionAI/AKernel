FROM akernel-bm1/all-in-one@sha256:75b136c8dd04a6e624eec8f88f6c8847dbbc3f4173a05823c0362080f90f72a1
# Node-candidate v2: replace ONLY sandboxd (combined-checkpoint fix 8f43b1cb).
# runsc (7d9c5612...), SIGRTMIN+3 stop signal, and every other byte of the
# rev29 node image stay untouched. Mode 0755 comes from the context file.
COPY sandboxd /usr/local/bin/sandboxd
