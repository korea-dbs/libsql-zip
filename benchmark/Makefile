INCLUDE_FLAGS := -I../
COMPILER_FLAGS := -L ../.libs/ -llibsql -O2 $(INCLUDE_FLAGS)

all: diskann bruteforce no_vectors

anntest: anntest.c
	@cc anntest.c -o anntest $(COMPILER_FLAGS)
benchtest: benchtest.c
	@cc benchtest.c -o benchtest $(COMPILER_FLAGS)
blobtest: blobtest.c
	@cc blobtest.c -o blobtest $(COMPILER_FLAGS)

diskann.sql:
	@python3 workload.py diskann 64 1000 1000 > diskann.sql
diskann: benchtest diskann.sql
	@rm test.db || exit 0
	@LD_LIBRARY_PATH=../.libs/ ./benchtest diskann.sql test.db

bruteforce.sql:
	@python3 workload.py bruteforce 64 1000 1000 > bruteforce.sql
bruteforce: benchtest bruteforce.sql
	@rm test.db || exit 0
	@LD_LIBRARY_PATH=../.libs/ ./benchtest bruteforce.sql test.db

no_vectors.sql: 
	@python3 workload.py no_vectors 1000 1000 > no_vectors.sql
no_vectors: benchtest no_vectors.sql
	@rm test.db || exit 0
	@LD_LIBRARY_PATH=../.libs/ ./benchtest no_vectors.sql test.db
clean:
	-rm benchtest
	-rm blobtest
	-rm anntest
	-rm *.sql
	-rm *.db
	-rm *.db-*
	-rm *.db-*
