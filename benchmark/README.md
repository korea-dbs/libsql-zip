## benchmarks tools

Simple benchmark tools intentionally written in C in order to have faster feedback loops (no need to wait for Rust builds)

You need to install `numpy` for some scripts to work. You can do it globally or using virtual env:
```py
$> python -m venv .env
$> source .env/bin/activate
$> pip install -r requirements.txt
```

### benchtest

Simple generic tool which takes SQL file, db file and run all queries against provded DB file. 
For SQL file generation you can use/extend `workload.py` script.

Take a look at the example:
```sh
$> LD_LIBRARY_PATH=../.libs/ ./benchtest queries.sql data.db
open queries file at queries.sql
open sqlite db at 'data.db'
executed simple statement: 'CREATE TABLE t ( id INTEGER PRIMARY KEY, emb FLOAT32(4) );'
executed simple statement: 'CREATE INDEX t_idx ON t ( libsql_vector_idx(emb) );'
prepared statement: 'INSERT INTO t VALUES ( ?, vector(?) );'
inserts (queries.sql):
  insert: 710.25 micros (avg.), 4 (count)
  size  : 0.2695 MB
  reads : 1.00 (avg.), 4 (total)
  writes: 1.00 (avg.), 4 (total)
prepared statement: 'SELECT * FROM vector_top_k('t_idx', vector(?), ?);'
search (queries.sql):
  select: 63.25 micros (avg.), 4 (count)
  size  : 0.2695 MB
  reads : 1.00 (avg.), 4 (total)
```

It is linked against liblibsql.so which resides in the `../libs/` directory and must be explicitly built from `libsql-sqlite3` sources:
```sh
$> basename $(pwd)
libsql-sqlite3
$> make # this command will generate libs in the .libs directory
$> cd benchmark
$> make bruteforce
open queries file at bruteforce.sql
open sqlite db at 'test.db'
executed simple statement: 'PRAGMA journal_mode=WAL;'
executed simple statement: 'CREATE TABLE x ( id INTEGER PRIMARY KEY, embedding FLOAT32(64) );'
prepared statement: 'INSERT INTO x VALUES (?, vector(?));'
inserts (bruteforce.sql):
  insert: 46.27 micros (avg.), 1000 (count)
  size  : 0.2695 MB
  reads : 1.00 (avg.), 1000 (total)
  writes: 1.00 (avg.), 1000 (total)
prepared statement: 'SELECT id FROM x ORDER BY vector_distance_cos(embedding, vector(?)) LIMIT ?;'
search (bruteforce.sql):
  select: 329.32 micros (avg.), 1000 (count)
  size  : 0.2695 MB
  reads : 2000.00 (avg.), 2000000 (total)
```

### anntest

Simple tool which takes DB file with 2 tables `data (id INTEGER PRIMARY KEY, emb FLOAT32(n))` and `queries (emb FLOAT32(n))` and execute vector search for all vectors in `queries` table abainst `data` table using provided SQL statements. 

In order to generate DB file you can use `benchtest` with `workload.py` tools. Take a look at the example:
```sh
$> python workload.py recall_uniform 64 1000 1000 > recall_uniform.sql
$> LD_LIBRARY_PATH=../.libs/ ./benchtest recall_uniform.sql recall_uniform.db
$> # ./anntext [db path] [test name (used only for printed stats)] [ann query] [exact query]
$> LD_LIBRARY_PATH=../.libs/ ./anntest recall_uniform.db 10-recall@10 "SELECT rowid FROM vector_top_k('data_idx', ?, 10)" "SELECT id FROM data ORDER BY vector_distance_cos(emb, ?) LIMIT 10"
open sqlite db at 'recall_uniform.db'
ready to perform 1000 queries with SELECT rowid FROM vector_top_k('data_idx', ?, 10) ann query and SELECT id FROM data ORDER BY vector_distance_cos(emb, ?) LIMIT 10 exact query
88.91% 10-recall@10 (avg.)
```

### blobtest

Simple tool which aims to prove that `sqlite3_blob_reopen` API can substantially increase performance of reads.

Take a look at the example:
```sh
$> LD_LIBRARY_PATH=../.libs/ ./blobtest blob-read-simple.db read simple 1000 1000
open sqlite db at 'blob-read-simple.db'
blob table: ready to prepare
blob table: prepared
time: 3.76 micros (avg.), 1000 (count)
$> LD_LIBRARY_PATH=../.libs/ ./blobtest blob-read-reopen.db read reopen 1000 1000
open sqlite db at 'blob-read-reopen.db'
blob table: ready to prepare
blob table: prepared
time: 0.31 micros (avg.), 1000 (count)
```


---
## [DBS] sqlite3 libsql Recall Benchmark

### How To Use

1. Build sqlite3 libsql:
    ```
    ./configure 
    make -j
    ```

2. goto benchmark dir:
    ```
    cd benchmark
    ```

#### Mode 1: Pre-made SQL files (GloVe dataset)

Use pre-made insert/query SQL files from `dataset/`:

```
python3 libsql_test_recall.py \
  --insert-sql dataset/insert100k_glove.sql \
  --query-sql dataset/query10k_glove.sql \
  --groundtruth dataset/groundtruth_10k_k10_glove.txt \
  --shell ../sqlite3 \
  --db /mnt/nvme0/libsql_test_recall_glove.db \
  --k 10
```

Skip insert if DB already built:
```
python3 libsql_test_recall.py \
  --load-db /mnt/nvme0/{db_name} \
  --query-sql dataset/query10k_glove.sql \
  --shell ../sqlite3 \
  --k 10
```

#### Mode 2: Random vectors (insert random vectors)

```
python3 libsql_test_recall.py --shell ../sqlite3 --dim 1024 --n 5000 --k 10 --q 100
```

With compression:
```
python3 libsql_test_recall.py --shell ../sqlite3 --dim 1024 --n 5000 --k 10 --q 100 \
  --compress float8 --max-neighbors 20
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--dim` | 64 | Vector dimension |
| `--n` | 1000 | Number of data vectors to insert |
| `--k` | 10 | Top-k neighbors to retrieve |
| `--q` | 100 | Number of query vectors |
| `--shell` | `../sqlite3` | Path to sqlite3 shell binary |
| `--db` | `test_recall.db` | Database file path |
| `--sqldir` | `sql_recall` | Directory to save/reuse generated SQL files |
| `--compress` | None | Compress neighbors: `float32`, `float16`, `float8`, `float1bit` |
| `--max-neighbors` | None | Max neighbors for DiskANN index |
| `--tx` | 100 | Rows per transaction (1 = autocommit) |
| `--save-insert` | off | Save insert SQL to sqldir for reuse |
| `--load-db` | None | Path to existing DB file (skips insert step) |
| `--insert-sql` | None | Pre-made insert SQL file |
| `--query-sql` | None | Pre-made query SQL file |
| `--vecfile` | None | Vector text file (word vec1 vec2 ... per line) |
| `--seed` | 42 | Random seed |

---
## Dataset

GloVe datasets are in the `lsm_2/benchmark/dataset`

- `insert100k_glove.sql` - 100K inserts, 200-dim word vectors from GloVe dataset
- `query10k_glove.sql` - 10K vector_top_k queries (k=10)
- `groundtruth_10k_k10_glove.txt` - ground truth for the `query10k_glove.sql`

Generated from `wiki_giga_2024_200_MFT20_vectors_seed_2024_alpha_0.75_eta_0.05_combined.txt` (1.29M word vectors, 200 dimensions).

---
**To Convert SIFT1M dataset to SQL files for sqlite3_libsql benchmark**

Download first:
```
  wget ftp://ftp.irisa.fr/local/texmex/corpus/sift.tar.gz
  tar xzf sift.tar.gz
```
Usage:
```
  python3 sift1m_to_sql.py --sift-dir sift --n 100000 --q 10000 --k 10 --outdir dataset
```
This generates:

  1. `dataset/insert_{n}_sift.sql`   - INSERT statements
  2. `dataset/query_{q}_sift.sql`    - vector_top_k queries
  3. `dataset/groundtruth_{q}_sift.txt` - groundtruth neighbor IDs (from .ivecs)

After generate, run:
```
python3 libsql_test_recall.py \
  --insert-sql dataset/insert_100000_sift.sql \
  --query-sql dataset/query_10000_sift.sql \
  --groundtruth dataset/groundtruth_10000_k10_sift.txt \
  --shell ../sqlite3 \
  --db /mnt/nvme0/libsql_test_recall_sift.db \
  --k 10
```