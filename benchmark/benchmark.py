import subprocess
import argparse
import os
import re
import select
import time
import threading
from datetime import datetime

TIME_MARKER = "__TIME__"
DISK_DEVICE = "mmcblk0p1"
TOP_K = 10


def _diskstats_devices():
    devices = set()
    try:
        with open("/proc/diskstats") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3:
                    devices.add(parts[2])
    except OSError:
        pass
    return devices


def _existing_path_for_mount(path):
    path = os.path.abspath(path)
    while not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            return "."
        path = parent
    return path


def detect_disk_device(path, fallback=DISK_DEVICE):
    devices = _diskstats_devices()
    if not devices:
        return fallback

    target = _existing_path_for_mount(path)
    try:
        proc = subprocess.run(
            ["findmnt", "-no", "SOURCE", "--target", target],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return fallback

    source = proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else ""
    candidates = []
    if source.startswith("/dev/"):
        candidates.append(os.path.basename(source))
        candidates.append(os.path.basename(os.path.realpath(source)))

        try:
            proc = subprocess.run(
                ["lsblk", "-no", "PKNAME", source],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=True,
            )
            candidates.extend(x.strip() for x in proc.stdout.splitlines() if x.strip())
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

    for device in candidates:
        if device in devices:
            return device
    return fallback


class DiskStatsMonitor:
    def __init__(self, device, interval_s=1.0, log_path=None):
        self.device = device
        self.interval_s = interval_s
        self.log_path = log_path
        self._stop_event = threading.Event()
        self._thread = None
        self._samples = []
        self._error = None
        self._prev = None
        self._prev_ts = None

    def start(self):
        first = self._read_stats()
        if first is None:
            self._error = f"device {self.device} not found in /proc/diskstats"
            return self
        self._prev = first
        self._prev_ts = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._thread is None:
            return self.summary()
        self._stop_event.set()
        self._thread.join(timeout=self.interval_s * 2 + 1.0)
        self._write_log()
        return self.summary()

    def _run(self):
        while not self._stop_event.wait(self.interval_s):
            cur = self._read_stats()
            ts = time.monotonic()
            if cur is None:
                self._error = f"device {self.device} disappeared from /proc/diskstats"
                return
            self._record_sample(cur, ts)
        cur = self._read_stats()
        ts = time.monotonic()
        if cur is not None:
            self._record_sample(cur, ts)

    def _record_sample(self, cur, ts):
        elapsed = ts - self._prev_ts
        if elapsed <= 0:
            self._prev = cur
            self._prev_ts = ts
            return
        d_read_reqs = cur["read_reqs"] - self._prev["read_reqs"]
        d_read_sectors = cur["read_sectors"] - self._prev["read_sectors"]
        d_read_ms = cur["read_ms"] - self._prev["read_ms"]
        d_write_reqs = cur["write_reqs"] - self._prev["write_reqs"]
        d_write_sectors = cur["write_sectors"] - self._prev["write_sectors"]
        d_write_ms = cur["write_ms"] - self._prev["write_ms"]
        d_busy_ms = cur["busy_ms"] - self._prev["busy_ms"]
        if min(d_read_reqs, d_read_sectors, d_read_ms, d_write_reqs,
               d_write_sectors, d_write_ms, d_busy_ms) < 0:
            self._prev = cur
            self._prev_ts = ts
            return
        self._samples.append({
            "wall_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_s": elapsed,
            "read_reqs": d_read_reqs,
            "read_bytes": d_read_sectors * 512,
            "read_ms": d_read_ms,
            "write_reqs": d_write_reqs,
            "write_bytes": d_write_sectors * 512,
            "write_ms": d_write_ms,
            "busy_ms": d_busy_ms,
        })
        self._prev = cur
        self._prev_ts = ts

    def _write_log(self):
        if not self.log_path:
            return
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        with open(self.log_path, "w") as fp:
            fp.write(
                "wall_time,elapsed_s,read_mbps,write_mbps,read_iops,write_iops,"
                "read_latency_ms,write_latency_ms,latency_ms,disk_util\n"
            )
            for s in self._samples:
                elapsed = s["elapsed_s"]
                read_mbps = s["read_bytes"] / (1024 * 1024) / elapsed if elapsed > 0 else 0.0
                write_mbps = s["write_bytes"] / (1024 * 1024) / elapsed if elapsed > 0 else 0.0
                read_iops = s["read_reqs"] / elapsed if elapsed > 0 else 0.0
                write_iops = s["write_reqs"] / elapsed if elapsed > 0 else 0.0
                read_latency = s["read_ms"] / s["read_reqs"] if s["read_reqs"] > 0 else 0.0
                write_latency = s["write_ms"] / s["write_reqs"] if s["write_reqs"] > 0 else 0.0
                total_reqs = s["read_reqs"] + s["write_reqs"]
                latency_ms = (s["read_ms"] + s["write_ms"]) / total_reqs if total_reqs > 0 else 0.0
                disk_util = s["busy_ms"] / (elapsed * 10.0) if elapsed > 0 else 0.0
                fp.write(
                    f"{s['wall_time']},{elapsed:.3f},{read_mbps:.3f},{write_mbps:.3f},"
                    f"{read_iops:.3f},{write_iops:.3f},{read_latency:.3f},{write_latency:.3f},"
                    f"{latency_ms:.3f},{disk_util:.3f}\n"
                )

    def _read_stats(self):
        try:
            with open("/proc/diskstats") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 14 or parts[2] != self.device:
                        continue
                    return {
                        "read_reqs": int(parts[3]),
                        "read_sectors": int(parts[5]),
                        "read_ms": int(parts[6]),
                        "write_reqs": int(parts[7]),
                        "write_sectors": int(parts[9]),
                        "write_ms": int(parts[10]),
                        "busy_ms": int(parts[12]),
                    }
        except OSError as e:
            self._error = str(e)
        return None

    def summary(self):
        if not self._samples:
            return {
                "device": self.device,
                "available": False,
                "error": self._error or "no diskstats samples captured",
            }
        total_elapsed = sum(s["elapsed_s"] for s in self._samples)
        total_read_bytes = sum(s["read_bytes"] for s in self._samples)
        total_write_bytes = sum(s["write_bytes"] for s in self._samples)
        total_read_reqs = sum(s["read_reqs"] for s in self._samples)
        total_write_reqs = sum(s["write_reqs"] for s in self._samples)
        total_read_ms = sum(s["read_ms"] for s in self._samples)
        total_write_ms = sum(s["write_ms"] for s in self._samples)
        total_busy_ms = sum(s["busy_ms"] for s in self._samples)
        mb = 1024 * 1024
        peak_read_mbps = max((s["read_bytes"] / mb / s["elapsed_s"]) for s in self._samples)
        peak_write_mbps = max((s["write_bytes"] / mb / s["elapsed_s"]) for s in self._samples)
        return {
            "device": self.device,
            "available": True,
            "log_path": self.log_path,
            "samples": len(self._samples),
            "elapsed_s": total_elapsed,
            "avg_read_mbps": total_read_bytes / mb / total_elapsed if total_elapsed > 0 else 0.0,
            "avg_write_mbps": total_write_bytes / mb / total_elapsed if total_elapsed > 0 else 0.0,
            "peak_read_mbps": peak_read_mbps,
            "peak_write_mbps": peak_write_mbps,
            "avg_read_iops": total_read_reqs / total_elapsed if total_elapsed > 0 else 0.0,
            "avg_write_iops": total_write_reqs / total_elapsed if total_elapsed > 0 else 0.0,
            "avg_read_latency_ms": total_read_ms / total_read_reqs if total_read_reqs > 0 else 0.0,
            "avg_write_latency_ms": total_write_ms / total_write_reqs if total_write_reqs > 0 else 0.0,
            "avg_latency_ms": (
                (total_read_ms + total_write_ms) / (total_read_reqs + total_write_reqs)
                if (total_read_reqs + total_write_reqs) > 0 else 0.0
            ),
            "avg_disk_util": total_busy_ms / (total_elapsed * 10.0) if total_elapsed > 0 else 0.0,
        }


def parse_zipcompact(stderr_text):
    """Extract ZIPCOMPACT_MS lines from stderr; returns (cleaned_stderr, dict or None).

    zipCompact() now runs once per WAL checkpoint in addition to once at close,
    so a single run can emit many ZIPCOMPACT_MS lines. Sum the time spent across
    all of them, track the largest BEFORE_MB seen (peak pre-compaction size,
    i.e. the worst-case disk usage this run reached) and the AFTER_MB of the
    last line (final on-disk size), and count how many actually did work.
    """
    stats = None
    kept = []
    for line in stderr_text.splitlines():
        m = re.match(
            r"ZIPCOMPACT_MS=([\d.]+)\s+BEFORE_MB=([\d.]+)\s+AFTER_MB=([\d.]+)", line
        )
        if m:
            ms = float(m.group(1))
            before_mb = float(m.group(2))
            after_mb = float(m.group(3))
            if stats is None:
                stats = {
                    "compact_ms": 0.0,
                    "before_mb": before_mb,
                    "after_mb": after_mb,
                    "n_compactions": 0,
                }
            stats["compact_ms"] += ms
            stats["before_mb"] = max(stats["before_mb"], before_mb)
            stats["after_mb"] = after_mb
            if ms > 0:
                stats["n_compactions"] += 1
        else:
            kept.append(line)
    cleaned = "\n".join(kept)
    if stderr_text.endswith("\n"):
        cleaned += "\n"
    return cleaned, stats


def parse_zipoffline(stderr_text):
    """Extract the ZIPOFFLINE_MS line from stderr; returns (cleaned_stderr, dict or None).

    Only emitted by "offline" zip-mode builds (LIBSQL_ZIP_MODE=offline): a
    single one-shot whole-file compression pass run at the end of the
    insert phase (see zipOfflineCompress() in os_unix.c), as opposed to
    "online" builds which compress incrementally as each page is written.
    """
    stats = None
    kept = []
    for line in stderr_text.splitlines():
        m = re.match(
            r"ZIPOFFLINE_MS=([\d.]+)\s+BEFORE_MB=([\d.]+)\s+AFTER_MB=([\d.]+)\s+"
            r"NUMPAGES=(\d+)",
            line,
        )
        if m:
            stats = {
                "compress_ms": float(m.group(1)),
                "before_mb": float(m.group(2)),
                "after_mb": float(m.group(3)),
                "num_pages": int(m.group(4)),
            }
        else:
            kept.append(line)
    cleaned = "\n".join(kept)
    if stderr_text.endswith("\n"):
        cleaned += "\n"
    return cleaned, stats


def parse_ovfl_zipcompact(stderr_text):
    """Same as parse_zipcompact() but for the overflow-only <db>-ovfl store
    (ZIPOVFLCOMPACT_MS= lines, emitted by zipOvflCompact() in os_unix.c)."""
    stats = None
    kept = []
    for line in stderr_text.splitlines():
        m = re.match(
            r"ZIPOVFLCOMPACT_MS=([\d.]+)\s+BEFORE_MB=([\d.]+)\s+AFTER_MB=([\d.]+)", line
        )
        if m:
            ms = float(m.group(1))
            before_mb = float(m.group(2))
            after_mb = float(m.group(3))
            if stats is None:
                stats = {
                    "compact_ms": 0.0,
                    "before_mb": before_mb,
                    "after_mb": after_mb,
                    "n_compactions": 0,
                }
            stats["compact_ms"] += ms
            stats["before_mb"] = max(stats["before_mb"], before_mb)
            stats["after_mb"] = after_mb
            if ms > 0:
                stats["n_compactions"] += 1
        else:
            kept.append(line)
    cleaned = "\n".join(kept)
    if stderr_text.endswith("\n"):
        cleaned += "\n"
    return cleaned, stats


def parse_ovfl_zipoffline(stderr_text):
    """Same as parse_zipoffline() but for the overflow-only + offline combo
    (ZIPOVFLOFFLINE_MS= line, emitted by zipOvflOfflineCompress() in
    os_unix.c). before_mb/after_mb here cover only the pages that were
    actually marked overflow and compressed this pass, not the whole file
    -- ordinary pages are left untouched on disk in this scope."""
    stats = None
    kept = []
    for line in stderr_text.splitlines():
        m = re.match(
            r"ZIPOVFLOFFLINE_MS=([\d.]+)\s+BEFORE_MB=([\d.]+)\s+AFTER_MB=([\d.]+)\s+"
            r"NUMPAGES=(\d+)",
            line,
        )
        if m:
            stats = {
                "compress_ms": float(m.group(1)),
                "before_mb": float(m.group(2)),
                "after_mb": float(m.group(3)),
                "num_pages": int(m.group(4)),
            }
        else:
            kept.append(line)
    cleaned = "\n".join(kept)
    if stderr_text.endswith("\n"):
        cleaned += "\n"
    return cleaned, stats


def parse_ovfl_zipmark(stderr_text):
    """Extract the ZIPOVFLMARK_MS line from stderr; returns (cleaned_stderr, dict or None).

    Isolates the cost of the SQLITE_FCNTL_ZIP_OVFL_MARK/UNMARK classification
    hook itself (called from btree.c on every overflow-page allocate/free),
    as distinct from actual compress/decompress or read/write I/O. Only
    meaningful for zip_scope="ovfl" builds; printed unconditionally by
    closeUnixFile() whenever any marking happened this connection, even in
    offline-mode's insert phase where the sidecar fd stays closed the whole
    time (see zipOvflSetMark() in os_unix.c).
    """
    stats = None
    kept = []
    for line in stderr_text.splitlines():
        m = re.match(
            r"ZIPOVFLMARK_MS=([\d.]+)\s+ZIPOVFLMARK_N=(\d+)\s+ZIPOVFLUNMARK_N=(\d+)",
            line,
        )
        if m:
            stats = {
                "mark_ms": float(m.group(1)),
                "mark_n": int(m.group(2)),
                "unmark_n": int(m.group(3)),
            }
        else:
            kept.append(line)
    cleaned = "\n".join(kept)
    if stderr_text.endswith("\n"):
        cleaned += "\n"
    return cleaned, stats


def parse_ziptime(stderr_text):
    """Extract ZIPCOMP_MS/ZIPDECOMP_MS line from stderr; returns (cleaned_stderr, dict or None).

    Cumulative time spent inside the page compressor/decompressor for this
    process (one shell invocation = one Insert phase or one Query phase),
    as opposed to the normal unixRead/unixWrite I/O time around it.
    """
    stats = None
    kept = []
    for line in stderr_text.splitlines():
        m = re.match(
            r"ZIPCOMP_MS=([\d.]+)\s+ZIPCOMP_N=(\d+)\s+"
            r"ZIPDECOMP_MS=([\d.]+)\s+ZIPDECOMP_N=(\d+)\s+"
            r"ZIPNUMPAGES=(\d+)\s+ZIPWRITECALLS=(\d+)\s+ZIPHOLECOUNT=(\d+)\s+"
            r"ZIPCKPTTOTAL_MS=([\d.]+)\s+ZIPCKPTCOMPACT_MS=([\d.]+)\s+"
            r"ZIPCLOSECOMPACT_MS=([\d.]+)",
            line,
        )
        if m:
            stats = {
                "compress_ms":     float(m.group(1)),
                "compress_n":      int(m.group(2)),
                "decompress_ms":   float(m.group(3)),
                "decompress_n":    int(m.group(4)),
                "num_pages":       int(m.group(5)),
                "write_calls":     int(m.group(6)),
                "hole_count":      int(m.group(7)),
                # Real total time spent inside sqlite3PagerCheckpoint() across
                # every checkpoint this process ran (lock wait + WAL frame
                # copy + xSync), not just the VFS-level fsync/compact addition.
                "ckpt_total_ms":   float(m.group(8)),
                # Of that total, how much was spent inside zipCompact() when
                # triggered by a checkpoint, vs. the one forced compaction at
                # the final close.
                "ckpt_compact_ms": float(m.group(9)),
                "close_compact_ms": float(m.group(10)),
            }
        else:
            kept.append(line)
    cleaned = "\n".join(kept)
    if stderr_text.endswith("\n"):
        cleaned += "\n"
    return cleaned, stats


def parse_ovfl_ziptime(stderr_text):
    """Same as parse_ziptime() but for the overflow-only <db>-ovfl store
    (ZIPOVFL*= line, emitted once per connection close by closeUnixFile()
    in os_unix.c's LIBSQL_ZIP_OVFL_ONLY block). Same dict shape as
    parse_ziptime() so callers don't need to branch on scope."""
    stats = None
    kept = []
    for line in stderr_text.splitlines():
        m = re.match(
            r"ZIPOVFLCOMP_MS=([\d.]+)\s+ZIPOVFLCOMP_N=(\d+)\s+"
            r"ZIPOVFLDECOMP_MS=([\d.]+)\s+ZIPOVFLDECOMP_N=(\d+)\s+"
            r"ZIPOVFLNUMPAGES=(\d+)\s+ZIPOVFLWRITECALLS=(\d+)\s+ZIPOVFLHOLECOUNT=(\d+)\s+"
            r"ZIPOVFLCKPTTOTAL_MS=([\d.]+)\s+ZIPOVFLCKPTCOMPACT_MS=([\d.]+)\s+"
            r"ZIPOVFLCLOSECOMPACT_MS=([\d.]+)",
            line,
        )
        if m:
            stats = {
                "compress_ms":      float(m.group(1)),
                "compress_n":       int(m.group(2)),
                "decompress_ms":    float(m.group(3)),
                "decompress_n":     int(m.group(4)),
                "num_pages":        int(m.group(5)),
                "write_calls":      int(m.group(6)),
                "hole_count":       int(m.group(7)),
                "ckpt_total_ms":    float(m.group(8)),
                "ckpt_compact_ms":  float(m.group(9)),
                "close_compact_ms": float(m.group(10)),
            }
        else:
            kept.append(line)
    cleaned = "\n".join(kept)
    if stderr_text.endswith("\n"):
        cleaned += "\n"
    return cleaned, stats


def format_ziptime_summary(ziptime_stats, phase_time_s):
    comp_s = ziptime_stats["compress_ms"] / 1000
    decomp_s = ziptime_stats["decompress_ms"] / 1000
    comp_pct = comp_s / phase_time_s * 100 if phase_time_s > 0 else 0.0
    decomp_pct = decomp_s / phase_time_s * 100 if phase_time_s > 0 else 0.0
    return (
        f"        Compress={comp_s:.3f}s ({comp_pct:.1f}% of phase, n={ziptime_stats['compress_n']})  "
        f"Decompress={decomp_s:.3f}s ({decomp_pct:.1f}% of phase, n={ziptime_stats['decompress_n']})"
    )


def parse_time_stats(stderr_text):
    stats = {}
    kept = []
    for line in stderr_text.splitlines():
        if line.startswith(TIME_MARKER):
            m = re.search(r"real=([\d.]+)\s+user=([\d.]+)\s+sys=([\d.]+)", line)
            if m:
                stats["real_s"] = float(m.group(1))
                stats["user_s"] = float(m.group(2))
                stats["sys_s"] = float(m.group(3))
        else:
            kept.append(line)
    cleaned = "\n".join(kept)
    if stderr_text.endswith("\n"):
        cleaned += "\n"
    return cleaned, stats


def run_shell(shell, db, sql_input, env=None):
    """Run SQL through shell in one session. Returns (stdout, stderr, time_stats)."""
    if not sql_input.rstrip().endswith(".quit"):
        sql_input = sql_input + "\n.quit\n"
    cmd = [shell, db]
    use_time = os.path.exists("/usr/bin/time")
    if use_time:
        cmd = ["/usr/bin/time", "-f", f"{TIME_MARKER} real=%e user=%U sys=%S"] + cmd
    proc = subprocess.run(
        cmd,
        input=sql_input,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=86400,
        env=env,
    )
    stderr_text, time_stats = parse_time_stats(proc.stderr)
    if proc.returncode != 0 and proc.returncode != 1:
        err_lines = stderr_text.splitlines()
        err_msg = "\n".join(err_lines[-10:]) if err_lines else stderr_text[-500:]
        raise RuntimeError(f"shell error (rc={proc.returncode}): {err_msg}")
    return proc.stdout, stderr_text, time_stats


def drop_caches(db_path=None, enabled=True):
    """Evict database files from OS page cache before a timed phase.

    Uses posix_fadvise(POSIX_FADV_DONTNEED) on the DB files — no root required.
    If no files exist yet (e.g. before the first insert), returns immediately
    since there is nothing cached.  Falls back to sudo drop_caches only if
    files exist but posix_fadvise failed for all of them.
    """
    if not enabled:
        return

    # Per-file eviction — no root required, works on any Linux system.
    if db_path and hasattr(os, 'posix_fadvise'):
        any_file = False
        evicted = False
        for suffix in ["", ".zipidx", "-wal", "-shm", "-ovfl", "-ovfl.zipidx"]:
            p = db_path + suffix
            if not os.path.exists(p):
                continue
            any_file = True
            try:
                size = os.path.getsize(p)
                if size > 0:
                    with open(p, 'rb') as f:
                        os.posix_fadvise(f.fileno(), 0, size, os.POSIX_FADV_DONTNEED)
                    evicted = True
            except OSError:
                pass
        if not any_file:
            return  # DB doesn't exist yet — nothing cached, nothing to do
        if evicted:
            return

    # Fallback: system-wide page cache drop (requires passwordless sudo).
    try:
        subprocess.run(["sudo", "sh", "-c", "sync; echo 3 > /proc/sys/vm/drop_caches"],
                       check=True, timeout=10)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired,
            PermissionError, OSError) as e:
        print(f"  WARNING: drop_caches failed: {e}")


def read_sql(sql_path):
    with open(sql_path) as f:
        return f.read()


def prepare_insert_sql(sql_text, page_size_kb):
    """Prepend PRAGMA page_size if requested."""
    if page_size_kb is None:
        return sql_text
    return f"PRAGMA page_size={page_size_kb * 1024};\n" + sql_text


def parse_output_to_results(output, k):
    """Parse shell output into list of sets of IDs, chunked by k."""
    id_lines = []
    for line in output.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            id_lines.append(int(line))
        except ValueError:
            pass
    results = []
    for i in range(0, len(id_lines), k):
        results.append(set(id_lines[i:i+k]))
    return results


def load_groundtruth(path):
    """Load groundtruth file: one line per query, comma-separated IDs."""
    results = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                ids = set(int(x) for x in line.split(",") if x.strip())
                results.append(ids)
    return results


def file_size_mb(path):
    try:
        return os.path.getsize(path) / (1024 * 1024)
    except OSError:
        return 0.0


def disk_usage_bytes(path):
    """Real on-disk footprint (actual allocated blocks), not the file's
    apparent/logical size returned by os.path.getsize()/stat().st_size.

    These two differ for the overflow-only zip scope: pages redirected to
    <db>-ovfl are never physically written into the main db file, but the
    file is still logically extended (ftruncate) to cover their offset so
    the pager's page-count math stays correct (see the LIBSQL_ZIP_OVFL_ONLY
    block in os_unix.c's unixWrite()). That leaves a sparse hole -- counted
    in st_size, not in st_blocks. st_blocks is always in 512-byte units per
    POSIX, regardless of the filesystem's actual block size.
    """
    try:
        return os.stat(path).st_blocks * 512
    except OSError:
        return 0


def total_disk_size_mb(db_path, zip_scope="full"):
    """Return (db_mb, ovfl_mb, idx_mb): real disk usage of the main db file,
    of the <db>-ovfl compressed data heap, and of the zip index file. All
    three are actual disk footprint (see disk_usage_bytes()), not apparent
    size.

    ovfl_mb is only nonzero for zip_scope="ovfl" (<db>-ovfl); the whole-db
    scope has no separate data heap since compressed pages live directly in
    the main db file. idx_mb is <db>-ovfl.zipidx for the ovfl scope, or
    <db>.zipidx for the whole-db scope -- the two scopes never coexist on
    the same db_path, so this single field covers both without ambiguity.
    """
    db_bytes = disk_usage_bytes(db_path) if os.path.exists(db_path) else 0
    if zip_scope == "ovfl":
        ovfl_path = db_path + "-ovfl"
        idx_path = db_path + "-ovfl.zipidx"
        ovfl_bytes = disk_usage_bytes(ovfl_path) if os.path.exists(ovfl_path) else 0
    else:
        idx_path = db_path + ".zipidx"
        ovfl_bytes = 0
    idx_bytes = disk_usage_bytes(idx_path) if os.path.exists(idx_path) else 0
    return (db_bytes / (1024 * 1024), ovfl_bytes / (1024 * 1024), idx_bytes / (1024 * 1024))


def cleanup_db(db_path):
    for suffix in ["", "-wal", "-shm", "-journal", ".zipidx", "-ovfl", "-ovfl.zipidx"]:
        p = db_path + suffix
        if os.path.exists(p):
            os.remove(p)


def parse_diskann_stats(stderr_text):
    """Parse DiskANN insert/query stats from stderr output."""
    stats = {}
    def grab(pattern, key, conv=float):
        m = re.search(pattern, stderr_text)
        if m:
            stats[key] = conv(m.group(1))

    # Insert breakdown
    grab(r'insert statement total:\s*([\d.]+)\s+ms', 'insert_stmt_total_ms')
    grab(r'VDBE work:\s*([\d.]+)\s+ms', 'insert_vdbe_work_ms')
    grab(r'statement finish:\s*([\d.]+)\s+ms', 'insert_stmt_finish_ms')
    grab(r'shell db close:\s*([\d.]+)\s+ms', 'shell_close_ms')
    grab(r'shell statements:\s*(\d+)', 'shell_stmt_count', int)
    grab(r'shell prepare:\s*([\d.]+)\s+ms', 'shell_prepare_ms')
    grab(r'shell step:\s*([\d.]+)\s+ms', 'shell_step_ms')
    grab(r'shell finalize:\s*([\d.]+)\s+ms', 'shell_finalize_ms')
    grab(r'shell other:\s*([\d.]+)\s+ms', 'shell_other_ms')
    grab(r'step top-level calls:\s*(\d+)', 'step_top_count', int)
    grab(r'step api total:\s*([\d.]+)\s+ms', 'step_api_ms')
    grab(r'step core total:\s*([\d.]+)\s+ms', 'step_core_ms')
    grab(r'step mutex enter:\s*([\d.]+)\s+ms', 'step_mutex_enter_ms')
    grab(r'step mutex leave:\s*([\d.]+)\s+ms', 'step_mutex_leave_ms')
    grab(r'step auto reset:\s*([\d.]+)\s+ms', 'step_auto_reset_ms')
    grab(r'step ready setup:\s*([\d.]+)\s+ms', 'step_ready_ms')
    grab(r'step vdbe list:\s*([\d.]+)\s+ms', 'step_vdbe_list_ms')
    grab(r'step vdbe exec:\s*([\d.]+)\s+ms', 'step_vdbe_exec_ms')
    grab(r'step profile:\s*([\d.]+)\s+ms', 'step_profile_ms')
    grab(r'step wal callback:\s*([\d.]+)\s+ms', 'step_wal_ms')
    grab(r'step transfer error:\s*([\d.]+)\s+ms', 'step_transfer_ms')
    grab(r'step api exit:\s*([\d.]+)\s+ms', 'step_api_exit_ms')
    grab(r'step reprepare:\s*([\d.]+)\s+ms', 'step_reprepare_ms')
    grab(r'step reset:\s*([\d.]+)\s+ms', 'step_reset_ms')
    grab(r'step wrapper other:\s*([\d.]+)\s+ms', 'step_wrapper_other_ms')
    grab(r'non-index insert remainder:\s*([\d.]+)\s+ms', 'non_index_insert_ms')
    grab(r'base table insert:\s*([\d.]+)\s+ms', 'non_index_insert_ms')
    grab(r'table insert:\s*([\d.]+)\s+ms', 'non_index_insert_ms')
    grab(r'shadow (?:row|table) insert:\s*([\d.]+)\s+ms', 'shadow_insert_ms')
    grab(r'vector index build:\s*([\d.]+)\s+ms', 'build_total_ms')
    grab(r'index build:\s*([\d.]+)\s+ms', 'build_total_ms')
    grab(r'graph build/update:\s*([\d.]+)\s+ms', 'graph_build_ms')
    grab(r'build graph traversal:\s*([\d.]+)\s+ms', 'build_traversal_ms')
    grab(r'build edge update:\s*([\d.]+)\s+ms', 'build_edge_update_ms')
    grab(r'build KV read path:\s*([\d.]+)\s+ms', 'build_read_ms')
    grab(r'build blob read path:\s*([\d.]+)\s+ms', 'build_read_ms')
    grab(r'build read I/O:\s*([\d.]+)\s+ms', 'build_read_ms')
    grab(r'build KV write path:\s*([\d.]+)\s+ms', 'build_write_ms')
    grab(r'build blob write path:\s*([\d.]+)\s+ms', 'build_write_ms')
    grab(r'build write I/O:\s*([\d.]+)\s+ms', 'build_write_ms')
    grab(r'build distance:\s*([\d.]+)\s+ms', 'build_dist_ms')
    # Query stats
    grab(r'total:\s*([\d.]+)\s+ms', 'search_total_ms')
    grab(r'context init:\s*([\d.]+)\s+ms', 'ctx_init_ms')
    grab(r'graph traversal:\s*([\d.]+)\s+ms', 'graph_ms')
    grab(r'query KV read path:\s*([\d.]+)\s+ms', 'query_read_ms')
    grab(r'query blob read path:\s*([\d.]+)\s+ms', 'query_read_ms')
    grab(r'query read I/O:\s*([\d.]+)\s+ms', 'query_read_ms')
    grab(r'blob open:\s*([\d.]+)\s+ms', 'blob_open_ms')
    grab(r'blob reopen:\s*([\d.]+)\s+ms', 'blob_reopen_ms')
    grab(r'blob read:\s*([\d.]+)\s+ms', 'blob_read_call_ms')
    m_cache = re.search(r'blob read:\s*[\d.]+\s+ms\s+\(cache hit/miss\s+(\d+)/(\d+)\)', stderr_text)
    if m_cache:
        stats['blob_cache_hits'] = int(m_cache.group(1))
        stats['blob_cache_misses'] = int(m_cache.group(2))
    grab(r'KV cursor open:\s*([\d.]+)\s+ms', 'kv_cursor_open_ms')
    grab(r'KV seek:\s*([\d.]+)\s+ms', 'kv_seek_ms')
    grab(r'KV data:\s*([\d.]+)\s+ms', 'kv_data_ms')
    grab(r'KV decode:\s*([\d.]+)\s+ms', 'kv_decode_ms')
    grab(r'KV memcpy:\s*([\d.]+)\s+ms', 'kv_memcpy_ms')
    grab(r'query distance:\s*([\d.]+)\s+ms', 'query_dist_ms')
    grab(r'result collect:\s*([\d.]+)\s+ms', 'result_ms')
    grab(r'context deinit:\s*([\d.]+)\s+ms', 'ctx_deinit_ms')
    grab(r'vector search total:\s*([\d.]+)\s+ms', 'vector_search_total_ms')
    grab(r'vector parse:\s*([\d.]+)\s+ms', 'vector_parse_ms')
    grab(r'index lookup/open:\s*([\d.]+)\s+ms', 'index_lookup_ms')
    grab(r'diskAnn call:\s*([\d.]+)\s+ms', 'diskann_call_ms')
    grab(r'vector cleanup:\s*([\d.]+)\s+ms', 'vector_cleanup_ms')
    grab(r'([\d.]+)\s+q/s', 'qps')
    return stats


def extract_c_stat_blocks(stderr_text):
    """Return formatted DiskANN stat blocks emitted by the C code."""
    lines = stderr_text.splitlines()
    blocks = []
    cur = []
    in_block = False

    for line in lines:
        stripped = line.rstrip()
        if stripped.startswith("=== diskAnn ") and stripped.endswith("==="):
            if cur:
                blocks.append("\n".join(cur))
                cur = []
            in_block = True
            cur.append(stripped)
            continue

        if in_block:
            cur.append(stripped)
            if stripped == "================================================":
                blocks.append("\n".join(cur))
                cur = []
                in_block = False

    if cur:
        blocks.append("\n".join(cur))

    return blocks


def format_io_summary(io_stats):
    if not io_stats.get("available"):
        return f"disk={io_stats.get('device', DISK_DEVICE)} unavailable ({io_stats.get('error', 'unknown error')})"
    return (
        f"disk={io_stats['device']} "
        f"RBW={io_stats['avg_read_mbps']:.1f}MB/s "
        f"WBW={io_stats['avg_write_mbps']:.1f}MB/s "
        f"RIOPS={io_stats['avg_read_iops']:.0f} "
        f"WIOPS={io_stats['avg_write_iops']:.0f} "
        f"Latency={io_stats['avg_latency_ms']:.2f}ms "
        f"Util={io_stats['avg_disk_util']:.1f}%"
    )


def run_one_config(label, shell, insert_sql_path, query_sql_path,
                   gt_results, k, db_dir, do_drop_cache=False,
                   page_size_kb=None, disk_device=DISK_DEVICE,
                   search_only=False, zip_mode="online", zip_scope="full"):
    db_path = os.path.join(db_dir, f"bench_{label}.db")
    # Both parsers produce the same dict shape, so nothing downstream needs
    # to branch on zip_scope beyond picking which one to call here.
    compact_parser = parse_ovfl_zipcompact if zip_scope == "ovfl" else parse_zipcompact
    ziptime_parser = parse_ovfl_ziptime if zip_scope == "ovfl" else parse_ziptime
    offline_parser = parse_ovfl_zipoffline if zip_scope == "ovfl" else parse_zipoffline
    # Classification-hook cost only exists for the ovfl scope (MARK/UNMARK
    # is the mechanism that tells overflow pages apart from ordinary ones);
    # there's nothing analogous to parse for full-db compression.
    mark_parser = parse_ovfl_zipmark if zip_scope == "ovfl" else (lambda s: (s, None))

    if not search_only:
        cleanup_db(db_path)
    elif not os.path.exists(db_path):
        raise FileNotFoundError(f"search-only DB not found: {db_path}")

    child_env = os.environ.copy()
    child_env["DISKANN_IO_TIMING"] = "1"

    result = {"label": label}
    n_phases = 2 if search_only else 3

    print(f"\n{'='*60}")
    print(f"  Config: {label}")
    print(f"  Shell:  {shell}")
    print(f"{'='*60}")

    result["insert_time_s"] = 0.0
    result["insert_time_stats"] = {}
    result["ins_stats"] = {}
    result["insert_cpu_eff"] = 0.0
    result["insert_ziptime"] = None
    result["query_ziptime"] = None
    result["db_disk_mb"] = 0.0
    result["ovfl_disk_mb"] = 0.0
    result["idx_disk_mb"] = 0.0
    result["total_disk_mb"] = 0.0
    result["query_cpu_eff"] = 0.0

    if search_only:
        db_mb, ovfl_mb, idx_mb = total_disk_size_mb(db_path, zip_scope)
        result["db_disk_mb"] = round(db_mb, 1)
        result["ovfl_disk_mb"] = round(ovfl_mb, 1)
        result["idx_disk_mb"] = round(idx_mb, 1)
        result["total_disk_mb"] = round(db_mb + ovfl_mb + idx_mb, 1)
        print(f"  Using existing DB: {db_path} ({result['total_disk_mb']:.1f} MB)")
    else:
        print(f"  [1/{n_phases}] Schema + Insert...")

        insert_sql = read_sql(insert_sql_path)
        drop_caches(db_path, do_drop_cache)
        insert_mon = DiskStatsMonitor(disk_device).start()
        t0 = time.time()
        ins_out, ins_err, ins_time = run_shell(shell, db_path, insert_sql, env=child_env)
        t_insert = time.time() - t0
        result["insert_disk_io"] = insert_mon.stop()

        ins_err, compact_stats = compact_parser(ins_err)
        ins_err, ziptime_stats = ziptime_parser(ins_err)
        ins_err, offline_stats = offline_parser(ins_err)
        ins_err, mark_stats = mark_parser(ins_err)
        result["insert_ziptime"] = ziptime_stats
        result["offline_compress"] = offline_stats
        result["insert_mark"] = mark_stats
        if zip_mode == "offline":
            # Offline builds never do incremental checkpoint-time compaction
            # (the zip layer isn't engaged until the one-shot compress pass
            # at the very end of this phase, see zipOfflineCompress()), so
            # these metrics don't apply -- always 0.
            ckpt_total_ms = ckpt_compact_ms = close_compact_ms = 0.0
        else:
            ckpt_total_ms = ziptime_stats.get("ckpt_total_ms", 0.0) if ziptime_stats else 0.0
            ckpt_compact_ms = ziptime_stats.get("ckpt_compact_ms", 0.0) if ziptime_stats else 0.0
            close_compact_ms = ziptime_stats.get("close_compact_ms", 0.0) if ziptime_stats else 0.0
        result["ckpt_total_s"] = round(ckpt_total_ms / 1000, 2)
        result["ckpt_compact_s"] = round(ckpt_compact_ms / 1000, 2)
        result["close_compact_s"] = round(close_compact_ms / 1000, 2)
        err_lines = [l for l in ins_err.splitlines() if l.startswith("Error:")]
        if err_lines:
            print(f"        !! {len(err_lines)} SQL errors during schema/insert:")
            for l in err_lines[:5]:
                print(f"           {l}")
            if len(err_lines) > 5:
                print(f"           ... ({len(err_lines)-5} more)")
            raise RuntimeError(f"schema/insert phase had {len(err_lines)} SQL errors")

        db_mb, ovfl_mb, idx_mb = total_disk_size_mb(db_path, zip_scope)
        result["insert_time_s"] = round(t_insert, 2)
        if zip_mode == "offline":
            result["compact_s"] = round(offline_stats["compress_ms"] / 1000, 2) if offline_stats else 0.0
        else:
            result["compact_s"] = round(compact_stats["compact_ms"] / 1000, 2) if compact_stats else 0.0
        result["db_disk_mb"] = round(db_mb, 1)
        result["ovfl_disk_mb"] = round(ovfl_mb, 1)
        result["idx_disk_mb"] = round(idx_mb, 1)
        result["total_disk_mb"] = round(db_mb + ovfl_mb + idx_mb, 1)
        result["insert_time_stats"] = ins_time
        ins_stats = parse_diskann_stats(ins_err)
        result["ins_stats"] = ins_stats

        size_str = f"{result['total_disk_mb']:.1f} MB"
        if zip_scope == "ovfl" and (ovfl_mb > 0 or idx_mb > 0):
            size_str += f" (db={db_mb:.1f} + ovfl={ovfl_mb:.1f} + idx={idx_mb:.1f})"
        elif zip_scope != "ovfl" and idx_mb > 0:
            size_str += f" (data={db_mb:.1f} + idx={idx_mb:.1f})"
        compact_s = result["compact_s"]
        insert_only_s = t_insert - compact_s
        if zip_mode == "offline" and offline_stats:
            before_mb = offline_stats["before_mb"]
            print(
                f"        insert={insert_only_s:.1f}s (plain)  "
                f"compress={compact_s:.1f}s (whole-file)  "
                f"total={t_insert:.1f}s, "
                f"{before_mb:.1f} MB → {size_str}"
            )
        elif compact_stats and compact_stats["compact_ms"] > 0:
            before_mb = compact_stats["before_mb"]
            n_compact = compact_stats.get("n_compactions", 0)
            print(
                f"        insert={insert_only_s:.1f}s  "
                f"compact={compact_s:.1f}s ({n_compact}x)  "
                f"total={t_insert:.1f}s, "
                f"peak {before_mb:.1f} MB → {size_str}"
            )
        else:
            print(f"        {t_insert:.1f}s, {size_str}")
        if mark_stats:
            mark_pct = (mark_stats["mark_ms"] / 1000 / t_insert * 100) if t_insert > 0 else 0.0
            print(
                f"        MarkHook={mark_stats['mark_ms']/1000:.3f}s "
                f"({mark_pct:.1f}% of phase, "
                f"mark_n={mark_stats['mark_n']} unmark_n={mark_stats['unmark_n']})"
            )
        if ziptime_stats:
            print(format_ziptime_summary(ziptime_stats, t_insert))
        if zip_mode != "offline" and (ckpt_total_ms > 0 or close_compact_ms > 0):
            print(
                f"        checkpoint total: {result['ckpt_total_s']:.2f}s  |  "
                f"checkpoint compaction: {result['ckpt_compact_s']:.2f}s  |  "
                f"close compaction: {result['close_compact_s']:.2f}s"
            )
        if ins_time:
            real = ins_time.get('real_s', 0)
            user = ins_time.get('user_s', 0)
            sys_ = ins_time.get('sys_s', 0)
            cpu_eff = (user + sys_) / real * 100 if real > 0 else 0.0
            result["insert_cpu_eff"] = round(cpu_eff, 1)
            print(
                f"        time: real={real:.2f}s  "
                f"user={user:.2f}s  sys={sys_:.2f}s  "
                f"cpu_eff={cpu_eff:.0f}%"
            )
        if ins_stats.get('build_total_ms') is not None:
            stmt_s = ins_stats.get('insert_stmt_total_ms', 0) / 1000
            finish_s = ins_stats.get('insert_stmt_finish_ms', 0) / 1000
            wal_s = ins_stats.get('step_wal_ms', 0) / 1000
            build_s = ins_stats.get('build_total_ms', 0) / 1000
            shadow_s = ins_stats.get('shadow_insert_ms', 0) / 1000
            graph_s = ins_stats.get('graph_build_ms', 0) / 1000
            traversal_s = ins_stats.get('build_traversal_ms', 0) / 1000
            edge_update_s = ins_stats.get('build_edge_update_ms', 0) / 1000
            read_s = ins_stats.get('build_read_ms', 0) / 1000
            write_s = ins_stats.get('build_write_ms', 0) / 1000
            dist_s = ins_stats.get('build_dist_ms', 0) / 1000
            print(
                f"        Stmt={stmt_s:.1f}s  Commit={finish_s:.1f}s  "
                f"Checkpt={wal_s:.1f}s  VecBuild={build_s:.1f}s  "
                f"Shadow={shadow_s:.1f}s  GraphBuild={graph_s:.1f}s  "
                f"BuildTrav={traversal_s:.1f}s  EdgeUpd={edge_update_s:.1f}s  "
                f"ReadPath={read_s:.1f}s  WritePath={write_s:.1f}s  Dist={dist_s:.1f}s"
            )
            if ins_stats.get('step_api_ms') is not None:
                print(
                    f"        StepApi={ins_stats.get('step_api_ms', 0)/1000:.1f}s  "
                    f"StepCore={ins_stats.get('step_core_ms', 0)/1000:.1f}s  "
                    f"StepExec={ins_stats.get('step_vdbe_exec_ms', 0)/1000:.1f}s  "
                    f"StepReady={ins_stats.get('step_ready_ms', 0)/1000:.1f}s  "
                    f"StepAutoReset={ins_stats.get('step_auto_reset_ms', 0)/1000:.1f}s  "
                    f"StepReset={ins_stats.get('step_reset_ms', 0)/1000:.1f}s  "
                    f"StepReprep={ins_stats.get('step_reprepare_ms', 0)/1000:.1f}s"
                )
                print(
                    f"        StepMutex={ins_stats.get('step_mutex_enter_ms', 0)/1000:.1f}s/"
                    f"{ins_stats.get('step_mutex_leave_ms', 0)/1000:.1f}s  "
                    f"StepProfile={ins_stats.get('step_profile_ms', 0)/1000:.1f}s  "
                    f"StepWal={ins_stats.get('step_wal_ms', 0)/1000:.1f}s  "
                    f"StepXfer={ins_stats.get('step_transfer_ms', 0)/1000:.1f}s  "
                    f"StepApiExit={ins_stats.get('step_api_exit_ms', 0)/1000:.1f}s  "
                    f"StepOther={ins_stats.get('step_wrapper_other_ms', 0)/1000:.1f}s"
                )
        print(f"        {format_io_summary(result['insert_disk_io'])}")
        for block in extract_c_stat_blocks(ins_err):
            print(block)

    # Query (timed)
    phase_q = 1 if search_only else 2
    print(f"  [{phase_q}/{n_phases}] Querying...")
    query_sql = read_sql(query_sql_path)

    drop_caches(db_path, do_drop_cache)
    query_mon = DiskStatsMonitor(disk_device).start()
    t0 = time.time()
    ann_out, q_err, q_time_stats = run_shell(shell, db_path, query_sql, env=child_env)
    t_query = time.time() - t0
    result["query_disk_io"] = query_mon.stop()

    q_err, q_ziptime_stats = ziptime_parser(q_err)
    q_err, q_mark_stats = mark_parser(q_err)
    result["query_ziptime"] = q_ziptime_stats
    result["query_mark"] = q_mark_stats

    q_err_lines = [l for l in q_err.splitlines() if l.startswith("Error:")]
    if q_err_lines:
        print(f"        !! {len(q_err_lines)} SQL errors during query:")
        for l in q_err_lines[:5]:
            print(f"           {l}")
        if len(q_err_lines) > 5:
            print(f"           ... ({len(q_err_lines)-5} more)")

    ann_results = parse_output_to_results(ann_out, k)
    q = len(ann_results)
    qps = q / t_query if t_query > 0 else 0
    result["query_time_s"] = round(t_query, 2)
    result["queries"] = q
    result["query_per_sec"] = round(qps, 1)
    result["query_time_stats"] = q_time_stats
    q_stats = parse_diskann_stats(q_err)
    result["q_stats"] = q_stats
    print(f"        {t_query:.2f}s ({qps:.0f} q/s), {q} queries returned")
    if q_mark_stats:
        mark_pct = (q_mark_stats["mark_ms"] / 1000 / t_query * 100) if t_query > 0 else 0.0
        print(
            f"        MarkHook={q_mark_stats['mark_ms']/1000:.3f}s "
            f"({mark_pct:.1f}% of phase, "
            f"mark_n={q_mark_stats['mark_n']} unmark_n={q_mark_stats['unmark_n']})"
        )
    if q_ziptime_stats:
        print(format_ziptime_summary(q_ziptime_stats, t_query))
    if q_time_stats:
        real = q_time_stats.get('real_s', 0)
        user = q_time_stats.get('user_s', 0)
        sys_ = q_time_stats.get('sys_s', 0)
        cpu_eff = (user + sys_) / real * 100 if real > 0 else 0.0
        result["query_cpu_eff"] = round(cpu_eff, 1)
        print(
            f"        time: real={real:.2f}s  "
            f"user={user:.2f}s  sys={sys_:.2f}s  "
            f"cpu_eff={cpu_eff:.0f}%"
        )
    if q_stats.get('graph_ms'):
        print(
            f"        SearchTotal={q_stats.get('search_total_ms', 0):.0f}ms  "
            f"CtxInit={q_stats.get('ctx_init_ms', 0):.0f}ms  "
            f"Graph={q_stats.get('graph_ms', 0):.0f}ms  "
            f"ReadPath={q_stats.get('query_read_ms', 0):.0f}ms  "
            f"QueryDist={q_stats.get('query_dist_ms', 0):.0f}ms  "
            f"Result={q_stats.get('result_ms', 0):.0f}ms  "
            f"CtxDeinit={q_stats.get('ctx_deinit_ms', 0):.0f}ms"
        )
        if q_stats.get('blob_read_call_ms') or q_stats.get('kv_seek_ms'):
            print(
                f"        BlobOpen={q_stats.get('blob_open_ms', 0):.0f}ms  "
                f"BlobReopen={q_stats.get('blob_reopen_ms', 0):.0f}ms  "
                f"BlobRead={q_stats.get('blob_read_call_ms', 0):.0f}ms  "
                f"KVSeek={q_stats.get('kv_seek_ms', 0):.0f}ms  "
                f"KVData={q_stats.get('kv_data_ms', 0):.0f}ms  "
                f"KVDecode={q_stats.get('kv_decode_ms', 0):.0f}ms  "
                f"KVMemcpy={q_stats.get('kv_memcpy_ms', 0):.0f}ms"
            )
        if q_stats.get('vector_search_total_ms'):
            print(
                f"        VecSearch={q_stats.get('vector_search_total_ms', 0):.0f}ms  "
                f"VecParse={q_stats.get('vector_parse_ms', 0):.0f}ms  "
                f"IdxLookup={q_stats.get('index_lookup_ms', 0):.0f}ms  "
                f"DiskAnnCall={q_stats.get('diskann_call_ms', 0):.0f}ms  "
                f"VecCleanup={q_stats.get('vector_cleanup_ms', 0):.0f}ms"
            )
    print(f"        {format_io_summary(result['query_disk_io'])}")
    for block in extract_c_stat_blocks(q_err):
        print(block)

    # Recall
    phase_r = phase_q + 1
    print(f"  [{phase_r}/{n_phases}] Computing recall@{k}...")
    n_compare = min(len(ann_results), len(gt_results))
    if n_compare == 0:
        recall = 0.0
        print(f"        WARNING: no results to compare")
    else:
        total_hits = 0
        total_possible = 0
        for ann, gt in zip(ann_results[:n_compare], gt_results[:n_compare]):
            total_hits += len(ann & gt)
            total_possible += len(gt)
        recall = total_hits / total_possible if total_possible > 0 else 0.0
        print(f"        recall@{k} = {recall:.4f} ({recall*100:.2f}%)")

    result["recall"] = round(recall, 4)
    return result


def main():
    parser = argparse.ArgumentParser(
        description="libsql-zip compression benchmark",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # vanilla libsql only
  python3 benchmark.py --libsql-dir ../libsql-zip --compressions none

  # compare vanilla vs lz4 (build libsql-zip with: make LIBSQL_COMPRESS=lz4 libsql-zip)
  python3 benchmark.py --libsql-dir ../libsql-zip --compressions none,lz4

  # all three compression algorithms (each libsql-zip must be pre-built and placed
  # as libsql-zip-lz4, libsql-zip-snappy, libsql-zip-zstd in --libsql-dir)
  python3 benchmark.py --libsql-dir ../libsql-zip --compressions none,lz4,snappy,zstd

  # "offline" variant: insert uncompressed, compress the whole file once at
  # the end of the insert phase, then query (build with:
  # make LIBSQL_COMPRESS=lz4 LIBSQL_ZIP_MODE=offline libsql-zip)
  python3 benchmark.py --libsql-dir ../libsql-zip --compressions lz4 --mode offline

  # "overflow-only" variant: only b-tree overflow pages are compressed, into
  # a separate <db>-ovfl store; ordinary pages stay plain (build with:
  # make LIBSQL_COMPRESS=lz4 LIBSQL_ZIP_SCOPE=ovfl libsql-zip)
  python3 benchmark.py --libsql-dir ../libsql-zip --compressions lz4 --zip-scope ovfl

Shell binary naming convention in --libsql-dir (tag = any combination of
"ovfl-"/"offline-" selected by --zip-scope/--mode, in that order):
  none    -> libsql          (vanilla, no compression)
  compressed -> libsql-zip-<tag><algo>  (or libsql-zip-<tag without trailing
                algo>, e.g. libsql-zip, libsql-zip-offline, libsql-zip-ovfl,
                libsql-zip-ovfl-offline, if only one algo variant is present)

  --mode online --zip-scope full (default):    libsql-zip-lz4
  --mode offline --zip-scope full:             libsql-zip-offline-lz4
  --mode online --zip-scope ovfl:              libsql-zip-ovfl-lz4
  --mode offline --zip-scope ovfl:             libsql-zip-ovfl-offline-lz4
        """,
    )
    parser.add_argument("--libsql-dir", type=str, default=".",
                        help="Directory containing libsql and libsql-zip* binaries (default: .)")
    parser.add_argument("--compressions", type=str, default="none",
                        help="Comma-separated compression modes to benchmark: "
                             "none,lz4,snappy,zstd (default: none)")
    parser.add_argument("--mode", type=str, default="online", choices=["online", "offline"],
                        help="Zip mode: 'online' compresses each page as it's "
                             "written (default); 'offline' inserts uncompressed "
                             "and compresses the whole file once at the end of "
                             "the insert phase (requires binaries built with "
                             "LIBSQL_ZIP_MODE=offline). No effect for compression=none.")
    parser.add_argument("--zip-scope", type=str, default="full", choices=["full", "ovfl"],
                        help="Compression scope: 'full' compresses every page "
                             "(default); 'ovfl' compresses only b-tree overflow "
                             "pages into a separate <db>-ovfl store, leaving "
                             "ordinary pages plain (requires binaries built with "
                             "LIBSQL_ZIP_SCOPE=ovfl). No effect for compression=none.")
    parser.add_argument("--dataset-dir", type=str, default=os.path.expanduser("./dataset"),
                        help="Directory with SQL files (default: ./dataset)")
    parser.add_argument("--datasets", type=str, default="sift,glove,coco,cohere",
                        help="Comma-separated dataset names (default: sift,glove,coco,cohere)")
    parser.add_argument("--db-dir", type=str, default=".")
    parser.add_argument("--page-sizes", type=str, default="4,16,32,64",
                        help="Comma-separated page sizes in KB (default: 4,16,32,64)")
    parser.add_argument("--no-drop-cache", dest="drop_cache", action="store_false",
                        help="Disable OS page cache eviction before each timed phase")
    parser.add_argument("--disk-device", type=str, default="auto",
                        help="Block device name from /proc/diskstats, or 'auto' to detect")
    parser.add_argument("--search-only", action="store_true",
                        help="Run search and recall only using existing bench_*.db files")
    parser.add_argument("--keep-db", action="store_true",
                        help="Keep generated bench_*.db files after all runs")
    parser.set_defaults(drop_cache=True)
    args = parser.parse_args()

    page_sizes_kb = [int(x) for x in args.page_sizes.split(",")]
    compression_modes = [x.strip().lower() for x in args.compressions.split(",")]

    valid_modes = {"none", "lz4", "snappy", "zstd"}
    bad = [m for m in compression_modes if m not in valid_modes]
    if bad:
        print(f"Error: unknown compression mode(s): {bad}. Choose from {valid_modes}")
        return 1

    # Resolve shell binaries for each compression mode.
    # Naming convention: libsql-zip-<tag><algo>, where <tag> is built from
    # --zip-scope ("ovfl-" if ovfl) followed by --mode ("offline-" if
    # offline), in that order -- e.g. "ovfl-offline-" for both combined.
    # Falls back to the tag-only binary name (no algo suffix) if that's all
    # that's present, e.g. libsql-zip, libsql-zip-offline, libsql-zip-ovfl.
    def resolve_shell(mode):
        d = args.libsql_dir
        if mode == "none":
            candidates = [os.path.join(d, "libsql"), os.path.join(d, "sqlite3")]
        else:
            tag = ""
            if args.zip_scope == "ovfl":
                tag += "ovfl-"
            if args.mode == "offline":
                tag += "offline-"
            fallback = os.path.join(d, f"libsql-zip-{tag.rstrip('-')}") if tag \
                else os.path.join(d, "libsql-zip")
            candidates = [os.path.join(d, f"libsql-zip-{tag}{mode}"), fallback]
        for p in candidates:
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
        return None

    # Build configs: (label, shell_path, page_size_kb)
    configs = []
    skipped_modes = []
    for mode in compression_modes:
        shell = resolve_shell(mode)
        if shell is None:
            print(f"Warning: no binary found for compression='{mode}' "
                  f"(zip mode={args.mode}, zip scope={args.zip_scope}) "
                  f"in {args.libsql_dir}, skipping")
            skipped_modes.append(mode)
            continue
        if mode == "none":
            label_prefix = "libsql"
        else:
            parts = [mode]
            if args.zip_scope == "ovfl":
                parts.append("ovfl")
            if args.mode == "offline":
                parts.append("offline")
            label_prefix = "-".join(parts)
        for ps_kb in page_sizes_kb:
            configs.append((f"{label_prefix}_{ps_kb}kb", shell, ps_kb))

    if not configs:
        print("Error: no valid configurations found.")
        return 1

    # Validate datasets
    dataset_names = [x.strip() for x in args.datasets.split(",")]
    datasets = []
    for name in dataset_names:
        insert_sql = os.path.join(args.dataset_dir, f"insert100k_{name}.sql")
        query_sql = os.path.join(args.dataset_dir, f"query10k_{name}.sql")
        gt_file = os.path.join(args.dataset_dir, f"groundtruth_{name}.txt")
        required = [query_sql, gt_file] if args.search_only else [insert_sql, query_sql, gt_file]
        missing = [f for f in required if not os.path.isfile(f)]
        if missing:
            print(f"Warning: skipping dataset '{name}', missing: {missing}")
            continue
        datasets.append((name, insert_sql, query_sql, gt_file))

    if not datasets:
        print("Error: no valid datasets found.")
        return 1

    disk_device = (
        detect_disk_device(args.db_dir) if args.disk_device == "auto" else args.disk_device
    )

    print(f"Datasets:     {', '.join(n for n, _, _, _ in datasets)}")
    print(f"Compressions: {', '.join(m for m in compression_modes if m not in skipped_modes)}")
    print(f"Zip mode:     {args.mode}")
    print(f"Zip scope:    {args.zip_scope}")
    print(f"Page sizes:   {', '.join(str(x) + ' KB' for x in page_sizes_kb)}")
    print(f"Configs:      {', '.join(cfg[0] for cfg in configs)}")
    print(f"Disk device:  /dev/{disk_device}" + (" (auto)" if args.disk_device == "auto" else ""))
    print(f"DB dir:       {args.db_dir}")
    print(f"Total runs:   {len(datasets) * len(configs)}")

    all_results = {}
    for ds_name, insert_sql, query_sql, gt_file in datasets:
        print(f"\n{'#'*70}")
        print(f"  DATASET: {ds_name}")
        print(f"{'#'*70}")

        gt_results = load_groundtruth(gt_file)
        print(f"  Loaded {len(gt_results)} groundtruth queries")

        ds_results = []
        for label, shell, ps_kb in configs:
            run_label = f"{ds_name}_{label}"
            insert_sql_prepared = None
            if not args.search_only:
                insert_sql_text = read_sql(insert_sql)
                prepared_sql = prepare_insert_sql(insert_sql_text, ps_kb)
                insert_sql_prepared = os.path.join(args.db_dir, f".schema_{run_label}.sql")
                with open(insert_sql_prepared, "w") as f:
                    f.write(prepared_sql + "\n")

            result = run_one_config(
                run_label, shell, insert_sql_prepared, query_sql,
                gt_results, TOP_K, args.db_dir,
                do_drop_cache=args.drop_cache,
                page_size_kb=ps_kb,
                disk_device=disk_device,
                search_only=args.search_only,
                zip_mode=args.mode,
                zip_scope=args.zip_scope,
            )
            ds_results.append(result)

            db_path = os.path.join(args.db_dir, f"bench_{run_label}.db")
            if insert_sql_prepared and os.path.exists(insert_sql_prepared):
                os.remove(insert_sql_prepared)
            if args.keep_db:
                print(f"  Kept DB {db_path}")
            else:
                cleanup_db(db_path)
                print(f"  Cleaned up {db_path}")

        all_results[ds_name] = ds_results

    # Summary table
    for ds_name, ds_results in all_results.items():
        ins_hdr = (
            (f"{'Overall':>8} {'Compress':>8} " if args.mode == "offline"
             else f"{'Overall':>8} {'Compact':>8} {'CkptTot':>8} {'CkptCpt':>8} {'CloseCpt':>8} ")
            + f"{'Stmt':>8} {'Commit':>8} {'Checkpt':>8} "
            f"{'VecBuild':>8} {'Shadow':>8} {'Trav':>8} {'EdgeUpd':>8} "
            f"{'ReadPath':>8} {'WritePath':>9} {'Dist':>8}"
        )
        ins_sub = (
            (f"{'(s)':>8} {'(s)':>8} " if args.mode == "offline"
             else f"{'(s)':>8} {'(s)':>8} {'(s)':>8} {'(s)':>8} {'(s)':>8} ")
            + f"{'(s)':>8} {'(s)':>8} {'(s)':>8} "
            f"{'(s)':>8} {'(s)':>8} {'(s)':>8} {'(s)':>8} "
            f"{'(s)':>8} {'(s)':>9} {'(s)':>8}"
        )
        q_hdr = (
            f"{'Overall':>8} {'Graph':>8} {'ReadPath':>8} {'Dist':>8} "
            f"{'Result':>8} {'Q/s':>8} {'Recall':>8}"
        )
        q_sub = (
            f"{'(s)':>8} {'(ms)':>8} {'(ms)':>8} {'(ms)':>8} "
            f"{'(ms)':>8} {'':>8} {'@k':>8}"
        )
        size_hdr = f"{'DB':>8} {'Ovfl':>8} {'Idx':>8} {'Total':>8}"
        size_sub = f"{'(MB)':>8} {'(MB)':>8} {'(MB)':>8} {'(MB)':>8}"
        cpu_hdr  = f"{'CPUi%':>6} {'CPUq%':>6}"
        cpu_sub  = f"{'(eff)':>6} {'(eff)':>6}"
        zip_hdr  = f"{'IComp':>7} {'IDecomp':>7} {'QComp':>7} {'QDecomp':>7}"
        zip_sub  = f"{'(s)':>7} {'(s)':>7} {'(s)':>7} {'(s)':>7}"

        # Sized to the longest actual label in this dataset's results (e.g.
        # "snappy-ovfl-offline_4kb" at 23 chars blows past a fixed 20-char
        # column and desyncs every column after it) with a floor of 20 so
        # short labels don't shrink the table below its previous width.
        label_w = max(
            [20] + [len(r['label'].replace(f"{ds_name}_", "")) for r in ds_results]
        ) + 1

        hdr = f"{'Config':>{label_w}} |{ins_hdr} |{q_hdr} | {size_hdr} | {cpu_hdr} | {zip_hdr}"
        sub = f"{'':>{label_w}} |{ins_sub} |{q_sub} | {size_sub} | {cpu_sub} | {zip_sub}"
        w = len(hdr)
        title = f"SUMMARY: {ds_name} (k={TOP_K})"
        print(f"\n{'='*w}")
        print(f"{title:^{w}}")
        print(f"{'='*w}")
        ins_w = len(ins_hdr)
        q_w = len(q_hdr)
        size_w = len(size_hdr)
        cpu_w = len(cpu_hdr)
        zip_w = len(zip_hdr)
        print(f"{'':>{label_w}} |{'--- Insert ---':^{ins_w}} |{'--- Query ---':^{q_w}} | {'--- Disk ---':^{size_w}} | {'-- CPU --':^{cpu_w}} | {'-- Compress/Decompress --':^{zip_w}}")
        print(hdr)
        print(sub)
        print(f"{'-'*w}")
        for r in ds_results:
            short_label = r['label'].replace(f"{ds_name}_", "")
            ist = r.get('ins_stats', {})
            stmt_s = ist.get('insert_stmt_total_ms', 0) / 1000
            finish_s = ist.get('insert_stmt_finish_ms', 0) / 1000
            wal_s = ist.get('step_wal_ms', 0) / 1000
            build_s = ist.get('build_total_ms', 0) / 1000
            shadow_s = ist.get('shadow_insert_ms', 0) / 1000
            traversal_s = ist.get('build_traversal_ms', 0) / 1000
            edge_update_s = ist.get('build_edge_update_ms', 0) / 1000
            read_s = ist.get('build_read_ms', 0) / 1000
            write_s = ist.get('build_write_ms', 0) / 1000
            dist_s = ist.get('build_dist_ms', 0) / 1000
            qst = r.get('q_stats', {})
            ins_cpu_pct = r.get('insert_cpu_eff', float('nan'))
            q_cpu_pct   = r.get('query_cpu_eff', float('nan'))
            compact_s = r.get('compact_s', 0.0)
            ckpt_total_s = r.get('ckpt_total_s', 0.0)
            ckpt_compact_s = r.get('ckpt_compact_s', 0.0)
            close_compact_s = r.get('close_compact_s', 0.0)
            ins_vals = (
                (
                    f"{r['insert_time_s']:>8.1f} "
                    f"{compact_s:>8.1f} "
                ) if args.mode == "offline" else (
                    f"{r['insert_time_s']:>8.1f} "
                    f"{compact_s:>8.1f} "
                    f"{ckpt_total_s:>8.1f} "
                    f"{ckpt_compact_s:>8.1f} "
                    f"{close_compact_s:>8.1f} "
                )
            ) + (
                f"{stmt_s:>8.1f} {finish_s:>8.1f} {wal_s:>8.1f} "
                f"{build_s:>8.1f} "
                f"{shadow_s:>8.1f} {traversal_s:>8.1f} {edge_update_s:>8.1f} "
                f"{read_s:>8.1f} "
                f"{write_s:>9.1f} {dist_s:>8.1f}"
            )
            q_vals = (
                f"{r['query_time_s']:>8.1f} "
                f"{qst.get('graph_ms', 0):>8.1f} "
                f"{qst.get('query_read_ms', 0):>8.1f} "
                f"{qst.get('query_dist_ms', 0):>8.1f} "
                f"{qst.get('result_ms', 0):>8.1f} "
                f"{r['query_per_sec']:>8.0f} {r['recall']:>8.4f}"
            )
            cpu_vals = (
                f"{ins_cpu_pct:>5.0f}% "
                f"{q_cpu_pct:>5.0f}%"
            )
            size_vals = (
                f"{r['db_disk_mb']:>8.1f} "
                f"{r.get('ovfl_disk_mb', 0.0):>8.1f} "
                f"{r['idx_disk_mb']:>8.1f} "
                f"{r['total_disk_mb']:>8.1f}"
            )
            izt = r.get('insert_ziptime') or {}
            qzt = r.get('query_ziptime') or {}
            zip_vals = (
                f"{izt.get('compress_ms', 0)/1000:>7.2f} "
                f"{izt.get('decompress_ms', 0)/1000:>7.2f} "
                f"{qzt.get('compress_ms', 0)/1000:>7.2f} "
                f"{qzt.get('decompress_ms', 0)/1000:>7.2f}"
            )
            print(f"{short_label:>{label_w}} |{ins_vals} |{q_vals} | {size_vals} | {cpu_vals} | {zip_vals}")
        print(f"{'='*w}")


if __name__ == "__main__":
    main()
