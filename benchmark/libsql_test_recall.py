#!/usr/bin/env python3
"""
Recall benchmark for vanilla-veclite (sqlite3-based) vector search.
Uses the sqlite3 shell directly.

Usage:
  # With pre-made SQL files:
  python3 libsql_test_recall.py --insert-sql dataset/insert100k_glove.sql --query-sql dataset/query10k_glove.sql \
                         --shell ../sqlite3 --db /mnt/nvme0/recall_veclite.db --k 10

  # With groundtruth (skip brute-force):
  python3 libsql_test_recall.py --insert-sql dataset/insert100k_glove.sql --query-sql dataset/query10k_glove.sql \
                         --groundtruth dataset/groundtruth_10k_k10_glove.txt \
                         --shell ../sqlite3 --db /mnt/nvme0/recall_veclite.db --k 10

  # Skip insert, use existing db:
  python3 libsql_test_recall.py --load-db /mnt/nvme0/recall_veclite.db --query-sql dataset/query10k_glove.sql \
                         --groundtruth dataset/groundtruth_10k_k10_glove.txt \
                         --shell ../sqlite3 --k 10
"""

import subprocess
import random
import argparse
import os
import time
import re


def run_shell(shell, db, sql_file):
    """Run a SQL file through the sqlite3 shell."""
    with open(sql_file, 'r') as f:
        proc = subprocess.run(
            [shell, db],
            stdin=f,
            capture_output=True,
            text=True,
        )
    if proc.returncode != 0:
        stdout_tail = "\n".join(proc.stdout.strip().split("\n")[-5:]) if proc.stdout else ""
        print(f"STDERR: {proc.stderr}")
        print(f"STDOUT (last 5 lines): {stdout_tail}")
        raise RuntimeError(f"sqlite3 shell failed (rc={proc.returncode}) on {sql_file}")
    return proc.stdout


def extract_vectors_from_query_sql(query_sql_path):
    """Extract vector strings from query SQL file for brute-force generation."""
    vectors = []
    with open(query_sql_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            m = re.search(r"vector(?:32)?\('(\[[^\]]+\])'\)", line)
            if m:
                vectors.append(m.group(1))
    return vectors


def main():
    parser = argparse.ArgumentParser(description="Recall benchmark for vanilla-veclite (sqlite3) vector search")
    parser.add_argument("--k", type=int, default=10, help="Top-k neighbors")
    parser.add_argument("--shell", type=str, default="../sqlite3", help="Path to sqlite3 shell")
    parser.add_argument("--db", type=str, default="test_recall.db", help="Database file path")
    parser.add_argument("--sqldir", type=str, default="dataset", help="Directory to save/reuse SQL files")
    parser.add_argument("--load-db", type=str, default=None, help="Path to existing db file (skip insert)")
    parser.add_argument("--insert-sql", type=str, default=None, help="Pre-made insert SQL file")
    parser.add_argument("--query-sql", type=str, required=True, help="Pre-made query SQL file (for ANN search)")
    parser.add_argument("--groundtruth", type=str, default=None, help="Groundtruth file (skip brute-force)")
    args = parser.parse_args()

    os.makedirs(args.sqldir, exist_ok=True)

    # --- Step 0: Parse query SQL ---
    ann_search_file = args.query_sql
    query_vectors = extract_vectors_from_query_sql(args.query_sql)
    n_queries = len(query_vectors)
    print(f"Using query SQL: {args.query_sql} ({n_queries} queries)")

    # Generate brute-force SQL from the same vectors
    bf_search_file = os.path.join(args.sqldir, "bf_search.sql")
    with open(bf_search_file, 'w') as f:
        for vec_str in query_vectors:
            f.write(f"SELECT id FROM x ORDER BY vector_distance_cos(embedding, vector('{vec_str}')) LIMIT {args.k};\n\n")
    print(f"  Generated brute-force SQL: {bf_search_file}")

    # --- Step 1: Insert data ---
    if args.load_db:
        args.db = args.load_db
        print(f"Using existing db: {args.db}")
    elif args.insert_sql:
        for f in [args.db, args.db + "-wal", args.db + "-shm", args.db + "-journal"]:
            if os.path.exists(f):
                os.remove(f)
        print(f"Inserting from {args.insert_sql}...")
        t0 = time.time()
        with open(args.insert_sql, 'r') as sql_f:
            proc = subprocess.run(
                [args.shell, args.db],
                stdin=sql_f,
                capture_output=True,
                text=True,
            )
        if proc.stderr:
            n_errors = proc.stderr.count("Error")
            if n_errors > 0:
                print(f"  Warning: {n_errors} SQL errors during insert (non-fatal)")
        t_insert = time.time() - t0
        print(f"  Insert: {t_insert:.2f}s")
    else:
        print("Error: provide --insert-sql or --load-db")
        return

    # --- Step 2: DiskANN search ---
    print(f"Running DiskANN queries (k={args.k})...")
    ann_results = {}
    with open(ann_search_file, 'r') as f:
        queries = [line.strip() for line in f if line.strip()]
    t0 = time.time()
    tmp = os.path.join(args.sqldir, "_tmp_query.sql")
    for qid, sql in enumerate(queries):
        with open(tmp, 'w') as f:
            f.write(sql + "\n")
        out = run_shell(args.shell, args.db, tmp)
        ids = set()
        for line in out.strip().split("\n"):
            line = line.strip()
            if line and line != "id":
                try:
                    ids.add(int(line))
                except ValueError:
                    pass
        ann_results[qid] = ids
        if (qid + 1) % 1000 == 0:
            print(f"  ANN: {qid+1}/{n_queries}...")
    t_ann = time.time() - t0
    print(f"  DiskANN search: {t_ann:.2f}s ({t_ann/n_queries*1e6:.0f} us/query)")

    # --- Step 3: Brute-force search or groundtruth ---
    t_bf = 0
    bf_results = {}
    if args.groundtruth:
        print(f"Loading groundtruth from {args.groundtruth}...")
        with open(args.groundtruth) as f:
            for qid, line in enumerate(f):
                line = line.strip()
                if line:
                    ids = set(int(x) for x in line.split(",") if x.strip())
                    bf_results[qid] = ids
        print(f"  Loaded {len(bf_results)} groundtruth entries")
    else:
        print(f"Running {n_queries} brute-force queries (k={args.k})...")
        with open(bf_search_file, 'r') as f:
            queries = [line.strip() for line in f if line.strip()]
        t0 = time.time()
        for qid, sql in enumerate(queries):
            with open(tmp, 'w') as f:
                f.write(sql + "\n")
            out = run_shell(args.shell, args.db, tmp)
            ids = set()
            for line in out.strip().split("\n"):
                line = line.strip()
                if line and line != "id":
                    try:
                        ids.add(int(line))
                    except ValueError:
                        pass
            bf_results[qid] = ids
            if (qid + 1) % 1000 == 0:
                print(f"  BF: {qid+1}/{n_queries}...")
        t_bf = time.time() - t0
        print(f"  Brute-force search: {t_bf:.2f}s ({t_bf/n_queries*1e6:.0f} us/query)")

    # Clean up temp query file
    if os.path.exists(tmp):
        os.remove(tmp)

    # --- Step 4: Compute recall ---
    print()
    print(f"{'qid':>5} {'hits':>5} / {'k':>3}  {'recall':>8}")
    print("-" * 30)
    total_hits = 0
    total_possible = 0
    for qid in range(n_queries):
        ann = ann_results.get(qid, set())
        bf = bf_results.get(qid, set())
        hits = len(ann & bf)
        possible = len(bf)
        r = hits / possible if possible > 0 else 0.0
        if n_queries <= 200:
            print(f"{qid:>5} {hits:>5} / {possible:>3}  {r:>8.4f}")
        total_hits += hits
        total_possible += possible

    recall = total_hits / total_possible if total_possible > 0 else 0.0

    print("-" * 30)
    print(f"{'TOTAL':>5} {total_hits:>5} / {total_possible:>3}  {recall:>8.4f}")
    print()
    print("=" * 50)
    print(f"  k={args.k}, q={n_queries}")
    print(f"  DiskANN search: {t_ann:.2f}s ({t_ann/n_queries*1e6:.0f} us/query)")
    print(f"  Brute-force search: {t_bf:.2f}s ({t_bf/n_queries*1e6:.0f} us/query)")
    print(f"  DiskANN hits: {total_hits} / {total_possible}")
    print(f"  Mean Recall: {recall:.4f} ({recall*100:.2f}%)")
    print("=" * 50)


if __name__ == "__main__":
    main()
