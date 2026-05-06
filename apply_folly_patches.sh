#!/bin/bash
# Apply IoUring kernel compat patches to the getdeps folly clone.
# Run this once the folly source has been cloned by getdeps.
set -euo pipefail

SCRATCH=/tmp/fbcode_builder_getdeps-ZhomeZrsebenchtop2ZCacheLibZbuildZfbcode_builder
FOLLY_REPO="$SCRATCH/repos/github.com-facebook-folly.git"
COMPAT_H="$FOLLY_REPO/folly/io/async/IoUringKernelCompat.h"
BACKEND_H="$FOLLY_REPO/folly/io/async/IoUringBackend.h"
ZCRX_H="$FOLLY_REPO/folly/io/async/IoUringZeroCopyBufferPool.h"
PBRING_H="$FOLLY_REPO/folly/io/async/IoUringProvidedBufferRing.h"

if [ ! -d "$FOLLY_REPO" ]; then
    echo "ERROR: Folly repo not yet cloned at $FOLLY_REPO"
    echo "Wait for getdeps to fetch it, then re-run this script."
    exit 1
fi

echo "==> Writing IoUringKernelCompat.h"
cat > "$COMPAT_H" << 'COMPAT_EOF'
#pragma once
// Kernel/liburing compatibility stubs for features not available on this system.
// Include this AFTER <liburing.h>.
#include <stdint.h>

#ifndef IORING_CQE_F_BUF_MORE
#define IORING_CQE_F_BUF_MORE (1U << 4)
#endif
#ifndef IOU_PBUF_RING_INC
#define IOU_PBUF_RING_INC 2
#endif
#ifndef IORING_OP_RECV_ZC
#define IORING_OP_RECV_ZC IORING_OP_LAST
#endif

// io_uring_set_iowait: added in liburing 2.8
#if !defined(IO_URING_VERSION_MAJOR) || \
    (IO_URING_VERSION_MAJOR == 2 && IO_URING_VERSION_MINOR < 8)
static inline void io_uring_set_iowait(struct io_uring* /*ring*/, bool /*iowait*/) {}
#endif

// ZCRX (zero-copy RX networking): kernel 6.7+ feature not in this kernel/liburing
#ifndef IORING_ZCRX_AREA_SHIFT
#define IORING_ZCRX_AREA_SHIFT 16
#define IORING_ZCRX_AREA_MASK ((1ULL << IORING_ZCRX_AREA_SHIFT) - 1)
struct io_uring_zcrx_rqe { uint64_t off; uint32_t len; uint32_t __pad; };
struct io_uring_zcrx_rq {
  uint32_t* khead; uint32_t* ktail;
  struct io_uring_zcrx_rqe* rqes;
  uint32_t rq_tail; uint32_t ring_entries;
};
struct io_uring_zcrx_cqe { uint64_t off; uint64_t __pad; };
struct io_uring_region_desc {
  uint64_t user_addr; uint64_t size;
  uint32_t flags; uint32_t id;
  uint64_t mmap_offset; uint64_t __resv[4];
};
struct io_uring_zcrx_area_reg {
  uint64_t addr; uint64_t len;
  uint64_t rq_area_token;
  uint32_t flags; uint32_t __resv[3];
};
struct io_uring_zcrx_ifq_offsets {
  uint32_t head; uint32_t tail; uint32_t rqes; uint32_t __pad[5];
};
struct io_uring_zcrx_ifq_reg {
  uint32_t if_idx; uint32_t if_rxq;
  uint32_t rq_entries; uint32_t flags;
  uint64_t area_ptr; uint64_t region_ptr;
  uint32_t zcrx_id; uint32_t rx_buf_len;
  struct io_uring_zcrx_ifq_offsets offsets;
  uint32_t __resv[4];
};
#define IO_URING_QUERY_ZCRX 1
#define IORING_REGISTER_QUERY 25
struct io_uring_query_zcrx {
  uint32_t register_flags; uint32_t nr_ctrl_opcodes;
  uint32_t features; uint32_t __pad;
};
struct io_uring_query_hdr {
  uint32_t query_op; uint32_t result;
  uint64_t query_data; uint64_t size;
};
#define ZCRX_REG_IMPORT (1U << 0)
#define ZCRX_CTRL_EXPORT 0
#define ZCRX_FEATURE_RX_PAGE_SIZE (1U << 0)
#define IORING_MEM_REGION_TYPE_USER 1
#define IORING_REGISTER_ZCRX_CTRL 26
struct zcrx_export_info { int zcrx_fd; uint32_t __pad; };
struct zcrx_ctrl {
  uint32_t zcrx_id; uint32_t op;
  union { struct zcrx_export_info zc_export; uint64_t __pad[4]; };
};
static inline int io_uring_register_ifq(
    struct io_uring* /*ring*/, struct io_uring_zcrx_ifq_reg* /*reg*/) {
  return -ENOSYS;
}
#endif // IORING_ZCRX_AREA_SHIFT
COMPAT_EOF

echo "==> Patching IoUringBackend.h"
# Replace the old RECV_ZC stub with the compat include
python3 - "$BACKEND_H" << 'PYEOF'
import sys, re

path = sys.argv[1]
with open(path) as f:
    content = f.read()

# Check if compat header is already included
if 'IoUringKernelCompat.h' in content:
    print("  Already patched, skipping.")
    sys.exit(0)

# Replace the old IORING_OP_RECV_ZC stub with the compat include
old = '#ifndef IORING_OP_RECV_ZC\n#define IORING_OP_RECV_ZC IORING_OP_LAST\n#endif'
new = '#include <folly/io/async/IoUringKernelCompat.h>'
if old in content:
    content = content.replace(old, new)
    with open(path, 'w') as f:
        f.write(content)
    print("  Replaced RECV_ZC stub with compat include.")
else:
    # Find the last #include in the liburing block and add after it
    # Insert after the net/if.h include near the liburing block
    marker = '#include <net/if.h>'
    if marker in content:
        content = content.replace(
            marker,
            marker + '\n#include <folly/io/async/IoUringKernelCompat.h>'
        )
        with open(path, 'w') as f:
            f.write(content)
        print("  Added compat include after net/if.h.")
    else:
        print("  WARNING: could not find insertion point in IoUringBackend.h")
        sys.exit(1)
PYEOF

echo "==> Patching IoUringZeroCopyBufferPool.h"
python3 - "$ZCRX_H" << 'PYEOF'
import sys

path = sys.argv[1]
try:
    with open(path) as f:
        content = f.read()
except FileNotFoundError:
    print(f"  File not found: {path}, skipping.")
    sys.exit(0)

if 'IoUringKernelCompat.h' in content:
    print("  Already patched, skipping.")
    sys.exit(0)

# Insert after the FOLLY_POP_WARNING that follows <liburing.h>
marker = 'FOLLY_POP_WARNING'
idx = content.find(marker)
if idx == -1:
    print("  WARNING: FOLLY_POP_WARNING not found, trying after <liburing.h>")
    marker = '#include <liburing.h>'
    idx = content.find(marker)
    if idx == -1:
        print("  ERROR: cannot find insertion point in ZeroCopyBufferPool.h")
        sys.exit(1)

insert_after = idx + len(marker)
# Find end of that line
eol = content.find('\n', insert_after)
content = content[:eol+1] + '#include <folly/io/async/IoUringKernelCompat.h>\n' + content[eol+1:]
with open(path, 'w') as f:
    f.write(content)
print("  Added compat include after FOLLY_POP_WARNING/liburing.h.")
PYEOF

echo "==> Patching IoUringProvidedBufferRing.h"
python3 - "$PBRING_H" << 'PYEOF'
import sys

path = sys.argv[1]
try:
    with open(path) as f:
        content = f.read()
except FileNotFoundError:
    print(f"  File not found: {path}, skipping.")
    sys.exit(0)

if 'IoUringKernelCompat.h' in content:
    print("  Already patched, skipping.")
    sys.exit(0)

marker = 'FOLLY_POP_WARNING'
idx = content.find(marker)
if idx == -1:
    print("  ERROR: FOLLY_POP_WARNING not found in IoUringProvidedBufferRing.h")
    sys.exit(1)

insert_after = idx + len(marker)
eol = content.find('\n', insert_after)
content = content[:eol+1] + '#include <folly/io/async/IoUringKernelCompat.h>\n' + content[eol+1:]
with open(path, 'w') as f:
    f.write(content)
print("  Added compat include after FOLLY_POP_WARNING.")
PYEOF

echo "==> All folly patches applied successfully."
echo "    Build should now compile folly without missing kernel API errors."
