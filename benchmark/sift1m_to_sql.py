#!/usr/bin/env python3
"""
Convert SIFT1M dataset to SQL files for sqlite4_libsql benchmark.

Download first:
  wget ftp://ftp.irisa.fr/local/texmex/corpus/sift.tar.gz
  tar xzf sift.tar.gz

Usage:
  python3 sift1m_to_sql.py --sift-dir sift --n 100000 --q 10000 --k 10 --outdir dataset

This generates:
  dataset/insert_{n}_sift.sql   - INSERT statements
  dataset/query_{q}_sift.sql    - vector_top_k queries
  dataset/groundtruth_{q}_sift.txt - groundtruth neighbor IDs (from .ivecs)
"""

import struct
import argparse
import os
import sys


def read_fvecs(filename, max_count=None):
    """Read .fvecs file: each vector = [4-byte int dim][dim x 4-byte float]."""
    vectors = []
    with open(filename, 'rb') as f:
        while True:
            buf = f.read(4)
            if len(buf) < 4:
                break
            dim = struct.unpack('<i', buf)[0]
            vec = struct.unpack(f'<{dim}f', f.read(dim * 4))
            vectors.append(vec)
            if max_count and len(vectors) >= max_count:
                break
    return vectors


def read_ivecs(filename, max_count=None):
    """Read .ivecs file: each vector = [4-byte int dim][dim x 4-byte int]."""
    vectors = []
    with open(filename, 'rb') as f:
        while True:
            buf = f.read(4)
            if len(buf) < 4:
                break
            dim = struct.unpack('<i', buf)[0]
            vec = struct.unpack(f'<{dim}i', f.read(dim * 4))
            vectors.append(vec)
            if max_count and len(vectors) >= max_count:
                break
    return vectors


def main():
    parser = argparse.ArgumentParser(description="Convert SIFT1M to SQL for sqlite4_libsql")
    parser.add_argument("--sift-dir", type=str, required=True, help="Path to extracted sift/ directory")
    parser.add_argument("--n", type=int, default=100000, help="Number of base vectors to insert (max 1M)")
    parser.add_argument("--q", type=int, default=10000, help="Number of queries (max 10K)")
    parser.add_argument("--k", type=int, default=10, help="Top-k for queries")
    parser.add_argument("--outdir", type=str, default="dataset", help="Output directory")
    args = parser.parse_args()

    os.makedirs(args.outdir, exist_ok=True)

    # Read base vectors
    base_file = os.path.join(args.sift_dir, "sift_base.fvecs")
    print(f"Reading base vectors from {base_file}...", file=sys.stderr)
    base_vecs = read_fvecs(base_file, max_count=args.n)
    dim = len(base_vecs[0])
    print(f"  Loaded {len(base_vecs)} vectors, dim={dim}", file=sys.stderr)

    # Read query vectors
    query_file = os.path.join(args.sift_dir, "sift_query.fvecs")
    print(f"Reading query vectors from {query_file}...", file=sys.stderr)
    query_vecs = read_fvecs(query_file, max_count=args.q)
    print(f"  Loaded {len(query_vecs)} queries", file=sys.stderr)

    # Read groundtruth
    gt_file = os.path.join(args.sift_dir, "sift_groundtruth.ivecs")
    print(f"Reading groundtruth from {gt_file}...", file=sys.stderr)
    groundtruth = read_ivecs(gt_file, max_count=args.q)
    print(f"  Loaded {len(groundtruth)} groundtruth entries (top-{len(groundtruth[0])} each)", file=sys.stderr)

    # Write insert SQL
    insert_file = os.path.join(args.outdir, f"insert_{args.n}_sift.sql")
    print(f"Writing {insert_file}...", file=sys.stderr)
    with open(insert_file, 'w') as f:
        f.write(f"PRAGMA journal_mode=WAL;\n")
        f.write(f"CREATE TABLE x(id INTEGER PRIMARY KEY, embedding FLOAT32({dim}));\n")
        f.write(f"CREATE INDEX x_idx ON x(libsql_vector_idx(embedding, 'metric=l2', 'compress_neighbors=float8', 'max_neighbors=20'));\n")
        for i, vec in enumerate(base_vecs):
            vec_str = ",".join(f"{v:.6f}" for v in vec)
            f.write(f"INSERT INTO x(id, embedding) VALUES ({i}, vector32('[{vec_str}]'));\n")
            if (i + 1) % 100000 == 0:
                print(f"  {i+1}/{args.n}...", file=sys.stderr)

    # Write query SQL
    query_sql_file = os.path.join(args.outdir, f"query_{args.q}_sift.sql")
    print(f"Writing {query_sql_file}...", file=sys.stderr)
    with open(query_sql_file, 'w') as f:
        for vec in query_vecs:
            vec_str = ",".join(f"{v:.6f}" for v in vec)
            f.write(f"SELECT id FROM vector_top_k('x_idx', vector32('[{vec_str}]'), {args.k});\n")

    # Write groundtruth (top-k IDs per query, filtered to only include IDs < n)
    gt_out_file = os.path.join(args.outdir, f"groundtruth_{args.q}_k{args.k}_sift.txt")
    print(f"Writing {gt_out_file}...", file=sys.stderr)
    with open(gt_out_file, 'w') as f:
        for i, gt in enumerate(groundtruth):
            # Filter to IDs that are in our subset (< args.n) and take top-k
            valid_ids = [gid for gid in gt if gid < args.n][:args.k]
            f.write(",".join(str(gid) for gid in valid_ids) + "\n")

    print(f"\nDone!", file=sys.stderr)
    print(f"  Insert: {insert_file} ({args.n} vectors, {dim}-dim)", file=sys.stderr)
    print(f"  Query:  {query_sql_file} ({args.q} queries, k={args.k})", file=sys.stderr)
    print(f"  Ground: {gt_out_file}", file=sys.stderr)


if __name__ == "__main__":
    main()
