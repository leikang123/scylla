# Copyright 2020-present ScyllaDB
#
# This file is part of Scylla.
#
# Scylla is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Scylla is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with Scylla.  If not, see <http://www.gnu.org/licenses/>.

# Tests for secondary indexes

import time
import pytest
from cassandra.protocol import SyntaxException, AlreadyExists, InvalidRequest, ConfigurationException, ReadFailure, WriteFailure
from cassandra.query import SimpleStatement
from cassandra_tests.porting import assert_rows

from util import new_test_table, unique_name

# A reproducer for issue #7443: Normally, when the entire table is SELECTed,
# the partitions are returned sorted by the partitions' token. When there
# is filtering, this order is not expected to change. Furthermore, when this
# filtering happens to use a secondary index, again the order is not expected
# to change.
def test_partition_order_with_si(cql, test_keyspace):
    schema = 'pk int, x int, PRIMARY KEY ((pk))'
    with new_test_table(cql, test_keyspace, schema) as table:
        # Insert 20 partitions, all of them with x=1 so that filtering by x=1
        # will yield the same 20 partitions:
        N = 20
        stmt = cql.prepare('INSERT INTO '+table+' (pk, x) VALUES (?, ?)')
        for i in range(N):
            cql.execute(stmt, [i, 1])
        # SELECT all the rows, and verify they are returned in increasing
        # partition token order (note that the token is a *signed* number):
        tokens = [row.system_token_pk for row in cql.execute('SELECT token(pk) FROM '+table)]
        assert len(tokens) == N
        assert sorted(tokens) == tokens
        # Now select all the partitions with filtering of x=1. Since all
        # rows have x=1, this shouldn't change the list of matching rows, and
        # also shouldn't check their order:
        tokens1 = [row.system_token_pk for row in cql.execute('SELECT token(pk) FROM '+table+' WHERE x=1 ALLOW FILTERING')]
        assert tokens1 == tokens
        # Now add an index on x, which allows implementing the "x=1"
        # restriction differently. With the index, "ALLOW FILTERING" is
        # no longer necessary. But the order of the results should
        # still not change. Issue #7443 is about the order changing here.
        cql.execute('CREATE INDEX ON '+table+'(x)')
        # "CREATE INDEX" does not wait until the index is actually available
        # for use. Reads immediately after the CREATE INDEX may fail or return
        # partial results. So let's retry until reads resume working:
        for i in range(100):
            try:
                tokens2 = [row.system_token_pk for row in cql.execute('SELECT token(pk) FROM '+table+' WHERE x=1')]
                if len(tokens2) == N:
                    break
            except ReadFailure:
                pass
            time.sleep(0.1)
        assert tokens2 == tokens

# Test which ensures that indexes for a query are picked by the order in which
# they appear in restrictions. That way, users can deterministically pick
# which indexes are used for which queries.
# Note that the order of picking indexing is not set in stone and may be
# subject to change - in which case this test case should be amended as well.
# The order tested in this case was decided as a good first step in issue
# #7969, but it's possible that it will eventually be implemented another
# way, e.g. dynamically based on estimated query selectivity statistics.
# Ref: #7969
@pytest.mark.xfail(reason="The order of picking indexes is currently arbitrary. Issue #7969")
def test_order_of_indexes(scylla_only, cql, test_keyspace):
    schema = 'p int primary key, v1 int, v2 int, v3 int'
    with new_test_table(cql, test_keyspace, schema) as table:
        cql.execute(f"CREATE INDEX my_v3_idx ON {table}(v3)")
        cql.execute(f"CREATE INDEX my_v1_idx ON {table}(v1)")
        cql.execute(f"CREATE INDEX my_v2_idx ON {table}((p),v2)")
        # All queries below should use the first index they find in the list
        # of restrictions. Tracing information will be consulted to ensure
        # it's true. Currently some of the cases below succeed, because the
        # order is not well defined (and may, for instance, change upon
        # server restart), but some of them fail. Once a proper ordering
        # is implemented, all cases below should succeed.
        def index_used(query, index_name):
            assert any([index_name in event.description for event in cql.execute(query, trace=True).get_query_trace().events])
        index_used(f"SELECT * FROM {table} WHERE v3 = 1", "my_v3_idx")
        index_used(f"SELECT * FROM {table} WHERE v3 = 1 and v1 = 2 allow filtering", "my_v3_idx")
        index_used(f"SELECT * FROM {table} WHERE p = 1 and v1 = 1 and v3 = 2 allow filtering", "my_v1_idx")
        index_used(f"SELECT * FROM {table} WHERE p = 1 and v3 = 1 and v1 = 2 allow filtering", "my_v3_idx")
        # Local indexes are still skipped if they cannot be used
        index_used(f"SELECT * FROM {table} WHERE v2 = 1 and v1 = 2 allow filtering", "my_v1_idx")
        index_used(f"SELECT * FROM {table} WHERE v2 = 1 and v3 = 2 and v1 = 3 allow filtering", "my_v3_idx")
        index_used(f"SELECT * FROM {table} WHERE v1 = 1 and v2 = 2 and v3 = 3 allow filtering", "my_v1_idx")
        # Local indexes are still preferred over global ones, if they can be used
        index_used(f"SELECT * FROM {table} WHERE p = 1 and v1 = 1 and v3 = 2 and v2 = 2 allow filtering", "my_v2_idx")
        index_used(f"SELECT * FROM {table} WHERE p = 1 and v2 = 1 and v1 = 2 allow filtering", "my_v2_idx")

# Indexes can be created without an explicit name, in which case a default name is chosen.
# However, due to #8620 it was possible to break the index creation mechanism by creating
# a properly named regular table, which conflicts with the generated index name.
def test_create_unnamed_index_when_its_name_is_taken(cql, test_keyspace):
    schema = 'p int primary key, v int'
    with new_test_table(cql, test_keyspace, schema) as table:
        try:
            cql.execute(f"CREATE TABLE {table}_v_idx_index (i_do_not_exist_in_the_base_table int primary key)")
            # Creating an index should succeed, even though its default name is taken
            # by the table above
            cql.execute(f"CREATE INDEX ON {table}(v)")
        finally:
            cql.execute(f"DROP TABLE {table}_v_idx_index")

# Indexed created with an explicit name cause a materialized view to be created,
# and this view has a specific name - <index-name>_index. If there happens to be
# a regular table (or another view) named just like that, index creation should fail.
def test_create_named_index_when_its_name_is_taken(scylla_only, cql, test_keyspace):
    schema = 'p int primary key, v int'
    with new_test_table(cql, test_keyspace, schema) as table:
        index_name = unique_name()
        try:
            cql.execute(f"CREATE TABLE {test_keyspace}.{index_name}_index (i_do_not_exist_in_the_base_table int primary key)")
            # Creating an index should fail, because it's impossible to create
            # its underlying materialized view, because its name is taken by a regular table
            with pytest.raises(InvalidRequest, match="already exists"):
                cql.execute(f"CREATE INDEX {index_name} ON {table}(v)")
        finally:
            cql.execute(f"DROP TABLE {test_keyspace}.{index_name}_index")

# Tests for CREATE INDEX IF NOT EXISTS
# Reproduces issue #8717.
def test_create_index_if_not_exists(cql, test_keyspace):
    with new_test_table(cql, test_keyspace, 'p int primary key, v int') as table:
        cql.execute(f"CREATE INDEX ON {table}(v)")
        # Can't create the same index again without "IF NOT EXISTS", but can
        # do it with "IF NOT EXISTS":
        with pytest.raises(InvalidRequest, match="duplicate"):
            cql.execute(f"CREATE INDEX ON {table}(v)")
        cql.execute(f"CREATE INDEX IF NOT EXISTS ON {table}(v)")
        cql.execute(f"DROP INDEX {test_keyspace}.{table.split('.')[1]}_v_idx")

        # Now test the same thing for named indexes. This is what broke in #8717:
        cql.execute(f"CREATE INDEX xyz ON {table}(v)")
        with pytest.raises(InvalidRequest, match="already exists"):
            cql.execute(f"CREATE INDEX xyz ON {table}(v)")
        cql.execute(f"CREATE INDEX IF NOT EXISTS xyz ON {table}(v)")
        cql.execute(f"DROP INDEX {test_keyspace}.xyz")

        # Exactly the same with non-lower case name.
        cql.execute(f'CREATE INDEX "CamelCase" ON {table}(v)')
        with pytest.raises(InvalidRequest, match="already exists"):
            cql.execute(f'CREATE INDEX "CamelCase" ON {table}(v)')
        cql.execute(f'CREATE INDEX IF NOT EXISTS "CamelCase" ON {table}(v)')
        cql.execute(f'DROP INDEX {test_keyspace}."CamelCase"')

        # Trying to create an index for an attribute that's already indexed,
        # but with a different name. The "IF NOT EXISTS" appears to succeed
        # in this case, but does not actually create the new index name -
        # only the old one remains.
        cql.execute(f"CREATE INDEX xyz ON {table}(v)")
        with pytest.raises(InvalidRequest, match="duplicate"):
            cql.execute(f"CREATE INDEX abc ON {table}(v)")
        cql.execute(f"CREATE INDEX IF NOT EXISTS abc ON {table}(v)")
        with pytest.raises(InvalidRequest):
            cql.execute(f"DROP INDEX {test_keyspace}.abc")
        cql.execute(f"DROP INDEX {test_keyspace}.xyz")

# Another test for CREATE INDEX IF NOT EXISTS: Checks what happens if an index
# with the given *name* already exists, but it's a different index than the
# one requested, i.e.,
#    CREATE INDEX xyz ON tbl(a)
#    CREATE INDEX IF NOT EXIST xyz ON tbl(b)
# Should the second command
# 1. Silently do nothing (because xyz already exists),
# 2. or try to create an index (because an index on tbl(b) doesn't yet exist)
#    and visibly fail when it can't because the name is already taken?
# Cassandra chose the first behavior (silently do nothing), Scylla chose the
# second behavior. We consider Cassandra's behavior to be *wrong* and
# unhelpful - the intention of the user was ensure that an index tbl(b)
# (an index on column b of table tbl) exists, and if we can't, an error
# message is better than silently doing nothing.
# So this test is marked "cassandra_bug" - passes on Scylla and xfails on
# Cassandra.
# Reproduces issue #9182
def test_create_index_if_not_exists2(cql, test_keyspace, cassandra_bug):
    with new_test_table(cql, test_keyspace, 'p int primary key, v1 int, v2 int') as table:
        index_name = unique_name()
        cql.execute(f"CREATE INDEX {index_name} ON {table}(v1)")
        # Obviously can't create a different index with the same name:
        with pytest.raises(InvalidRequest, match="already exists"):
            cql.execute(f"CREATE INDEX {index_name} ON {table}(v2)")
        # Even with "IF NOT EXISTS" we still get a failure. An index for
        # {table}(v2) does not yet exist, so the index creation is attempted.
        with pytest.raises(InvalidRequest, match="already exists"):
            cql.execute(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table}(v2)")

# Test that the paging state works properly for indexes on tables
# with descending clustering order. There was a problem with indexes
# created on clustering keys with DESC clustering order - they are represented
# as "reverse" types internally and Scylla assertions failed that the base type
# is different from the underlying view type, even though, from the perspective
# of deserialization, they're equal. Issue #8666
def test_paging_with_desc_clustering_order(cql, test_keyspace):
    schema = 'p int, c int, primary key (p,c)'
    extra = 'with clustering order by (c desc)'
    with new_test_table(cql, test_keyspace, schema, extra) as table:
        cql.execute(f"CREATE INDEX ON {table}(c)")
        for i in range(3):
            cql.execute(f"INSERT INTO {table}(p,c) VALUES ({i}, 42)")
        stmt = SimpleStatement(f"SELECT * FROM {table} WHERE c = 42", fetch_size=1)
        assert len([row for row in cql.execute(stmt)]) == 3

# Test that deleting a base partition works fine, even if it produces a large batch
# of individual view updates. Refs #8852 - view updates used to be applied with
# per-partition granularity, but after fixing the issue it's no longer the case,
# so a regression test is necessary. Scylla-only - relies on the underlying
# representation of the index table.
def test_partition_deletion(cql, test_keyspace, scylla_only):
    schema = 'p int, c1 int, c2 int, v int, primary key (p,c1,c2)'
    with new_test_table(cql, test_keyspace, schema) as table:
        cql.execute(f"CREATE INDEX ON {table}(c1)")
        prep = cql.prepare(f"INSERT INTO {table}(p,c1,c2) VALUES (1, ?, 1)")
        for i in range(1342):
            cql.execute(prep, [i])
        cql.execute(f"DELETE FROM {table} WHERE p = 1")
        res = [row for row in cql.execute(f"SELECT * FROM {table}_c1_idx_index")]
        assert len(res) == 0

# Test that deleting a clustering range works fine, even if it produces a large batch
# of individual view updates. Refs #8852 - view updates used to be applied with
# per-partition granularity, but after fixing the issue it's no longer the case,
# so a regression test is necessary. Scylla-only - relies on the underlying
# representation of the index table.
def test_range_deletion(cql, test_keyspace, scylla_only):
    schema = 'p int, c1 int, c2 int, v int, primary key (p,c1,c2)'
    with new_test_table(cql, test_keyspace, schema) as table:
        cql.execute(f"CREATE INDEX ON {table}(c1)")
        prep = cql.prepare(f"INSERT INTO {table}(p,c1,c2) VALUES (1, ?, 1)")
        for i in range(1342):
            cql.execute(prep, [i])
        cql.execute(f"DELETE FROM {table} WHERE p = 1 AND c1 > 5 and c1 < 846")
        res = [row.c1 for row in cql.execute(f"SELECT * FROM {table}_c1_idx_index")]
        assert sorted(res) == [x for x in range(1342) if x <= 5 or x >= 846]

# Test that trying to insert a value for an indexed column that exceeds 64KiB fails,
# because this value is too large to be written as a key in the underlying index
def test_too_large_indexed_value(cql, test_keyspace):
    schema = 'p int, c int, v text, primary key (p,c)'
    with new_test_table(cql, test_keyspace, schema) as table:
        cql.execute(f"CREATE INDEX ON {table}(v)")
        big = 'x'*66536
        with pytest.raises(WriteFailure):
            try:
                cql.execute(f"INSERT INTO {table}(p,c,v) VALUES (0,1,'{big}')")
            # Cassandra 4.0 uses a different error type - so a minor translation is needed
            except InvalidRequest as ir:
                raise WriteFailure(str(ir))

# Selecting values using only clustering key should require filtering, but work correctly
# Reproduces issue #8991
def test_filter_cluster_key(cql, test_keyspace):
    schema = 'p int, c1 int, c2 int, primary key (p, c1, c2)'
    with new_test_table(cql, test_keyspace, schema) as table:
        cql.execute(f"CREATE INDEX ON {table}(c2)")
        cql.execute(f"INSERT INTO {table} (p, c1, c2) VALUES (0, 1, 1)")
        cql.execute(f"INSERT INTO {table} (p, c1, c2) VALUES (0, 0, 1)")
        
        stmt = SimpleStatement(f"SELECT c1, c2 FROM {table} WHERE c1 = 1 and c2 = 1 ALLOW FILTERING")
        rows = cql.execute(stmt)
        assert_rows(rows, [1, 1])

def test_multi_column_with_regular_index(cql, test_keyspace):
    """Reproduces #9085."""
    with new_test_table(cql, test_keyspace, 'p int, c1 int, c2 int, r int, primary key(p,c1,c2)') as tbl:
        cql.execute(f'CREATE INDEX ON {tbl}(r)')
        cql.execute(f'INSERT INTO {tbl}(p, c1, c2, r) VALUES (1, 1, 1, 0)')
        cql.execute(f'INSERT INTO {tbl}(p, c1, c2, r) VALUES (1, 1, 2, 1)')
        cql.execute(f'INSERT INTO {tbl}(p, c1, c2, r) VALUES (1, 2, 1, 0)')
        assert_rows(cql.execute(f'SELECT c1 FROM {tbl} WHERE (c1,c2)<(2,0) AND r=0 ALLOW FILTERING'), [1])
        assert_rows(cql.execute(f'SELECT c1 FROM {tbl} WHERE p=1 AND (c1,c2)<(2,0) AND r=0 ALLOW FILTERING'), [1])
