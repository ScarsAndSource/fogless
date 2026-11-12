"""The Supabase-only code paths (keyset paging, bulk inserts, the balances function, undo, rollback), run
against the fake Supabase through the real HTTP routes -- always as a signed-in user, with other users'
data present, so every one of them is also a tenant-isolation check."""
import os
import sys
from decimal import Decimal as D

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from supa import FAKE, idx, g, User, Base  # noqa: E402


def in_ctx(user, fn, *a, **k):
    with idx.app.test_request_context():
        g.uid = user.id
        return fn(*a, **k)


def mine(user, method=None, table=None):
    """Recorded data requests that carried this user's id (as filter, stamped body, or RPC argument)."""
    out = []
    for m, t, params, body in FAKE.rest_requests:
        bodies = body if isinstance(body, list) else [body]
        owner = params.get("user_id") == f"eq.{user.id}" or any(
            isinstance(b, dict) and user.id in (b.get("user_id"), b.get("p_user_id")) for b in bodies)
        if owner and (method is None or m == method) and (table is None or t == table):
            out.append((m, t, params, body))
    return out


class TestSupabasePaths(Base):
    def setUp(self):
        self.u, self.other = User(), User()
        self.other.msg("999 other person's money upi")  # someone else's data must never show up in u's results
        FAKE.rest_requests.clear()

    def test_keyset_paging_returns_every_row_once(self):
        idx.SB_PAGE_SIZE = 3
        ids = [in_ctx(self.u, idx.add_transaction, "expense", i, "food", f"n{i}", "cash") for i in range(1, 9)]
        FAKE.rest_requests.clear()
        got = [r["id"] for r in in_ctx(self.u, idx._all_transactions)]
        self.assertEqual(got, sorted(ids, reverse=True))
        self.assertEqual([r["id"] for r in in_ctx(self.u, idx._all_transactions, order="id.asc")], sorted(ids))
        self.assertEqual(len(in_ctx(self.u, idx._all_transactions, limit=5)), 5)
        gets = mine(self.u, "GET", "transactions")
        self.assertTrue(gets)
        self.assertTrue(all("offset" not in p for _m, _t, p, _b in gets))

    def test_rpc_balances_match_slow_path_and_are_per_user(self):
        self.u.msg("/setbalance cash 1000")
        self.u.msg("gave rahul 200 cash")
        self.u.msg("500 dinner upi split with amit")
        FAKE.rest_requests.clear()
        fast = in_ctx(self.u, idx.get_balances_by_method)
        self.assertTrue(mine(self.u, "POST", "rpc/bucket_balances"))
        self.assertTrue(all(b == {"p_user_id": self.u.id} for _m, _t, _p, b in
                            [x for x in FAKE.rest_requests if x[1] == "rpc/bucket_balances"]))
        other_fast = in_ctx(self.other, idx.get_balances_by_method)

        FAKE.rpc_installed, idx._RPC["ok"] = False, None
        slow = in_ctx(self.u, idx.get_balances_by_method)
        other_slow = in_ctx(self.other, idx.get_balances_by_method)
        nz = lambda d: {k: v for k, v in d.items() if v}  # noqa: E731
        self.assertEqual(nz(fast), nz(slow))
        self.assertEqual(nz(other_fast), nz(other_slow))
        self.assertIs(idx._RPC["ok"], False)  # remembered: no more doomed RPC calls
        self.assertEqual(nz(other_fast), {"upi": D("-999.00")})  # none of u's money, only their own
        self.assertEqual(nz(fast).get("iou:rahul"), D("200.00"))
        self.assertEqual(nz(fast).get("iou:amit"), D("250.00"))

    def test_app_works_when_the_function_is_not_installed(self):
        FAKE.rpc_installed = False
        self.u.msg("100 lunch upi")
        self.assertAlmostEqual(self.u.balances()["balances"]["upi"], -100.0)
        self.assertAlmostEqual(self.u.state()["balance"], -100.0)

    def test_only_allow_listed_functions_can_be_called(self):
        # the user id is injected by _sb_rpc and only these functions exist for the app
        self.assertRaises(idx.StorageError, lambda: in_ctx(self.u, idx._sb_rpc, "something_else"))

    def test_split_is_two_inserts_and_rolls_back(self):
        self.u.msg("100 dinner split with a and b and c")
        posts = [x for x in mine(self.u, "POST", "transactions")]
        self.assertEqual(len(posts), 2)  # my share + ONE bulk insert of 3 IOUs (was 4 sequential writes)
        self.assertIsInstance(posts[1][3], list)
        self.assertEqual(len(posts[1][3]), 3)
        for r in list(FAKE.rows_for("transactions", self.u.id)):
            FAKE.tables["transactions"].remove(r)
        FAKE.fail_bulk_transactions = True
        r = self.u.msg("100 dinner split with a and b")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(FAKE.rows_for("transactions", self.u.id), [])  # my-share expense rolled back
        self.assertEqual(len(FAKE.rows_for("transactions", self.other.id)), 1)  # nobody else touched

    def test_multi_item_is_one_bulk_insert_in_order(self):
        self.u.msg("120 lunch upi, 50 bus cash, 200 veggies card")
        posts = mine(self.u, "POST", "transactions")
        self.assertEqual(len(posts), 1)
        rows = sorted(FAKE.rows_for("transactions", self.u.id), key=lambda r: r["id"])
        self.assertEqual([float(r["amount"]) for r in rows], [120.0, 50.0, 200.0])

    def test_undo_reverts_the_newest_thing_across_tables(self):
        self.u.msg("put 500 into goa pool")
        self.u.msg("300 from goa pool")
        self.assertIn("Reverted", self.u.msg("/undo").json["text"])
        self.assertEqual(FAKE.rows_for("pool_expenses", self.u.id), [])
        self.assertEqual(len(FAKE.rows_for("pool_contributions", self.u.id)), 1)
        self.u.msg("/undo")
        self.assertEqual(FAKE.rows_for("pool_contributions", self.u.id), [])
        self.assertEqual(FAKE.rows_for("transactions", self.u.id), [])
        self.assertEqual(len(FAKE.rows_for("transactions", self.other.id)), 1)  # the other user's ledger is untouched

    def test_undo_never_reaches_another_users_pool(self):
        self.other.msg("put 500 into goa pool")
        before = (len(FAKE.rows_for("pool_contributions", self.other.id)), len(FAKE.rows_for("transactions", self.other.id)))
        self.assertIn("Nothing to undo", self.u.msg("/undo").json["text"])
        after = (len(FAKE.rows_for("pool_contributions", self.other.id)), len(FAKE.rows_for("transactions", self.other.id)))
        self.assertEqual(before, after)

    def test_today_spend_only_downloads_expenses(self):
        self.u.msg("100 lunch")
        FAKE.rest_requests.clear()
        in_ctx(self.u, idx.get_today_expense)
        self.assertTrue(any(p.get("type") == "eq.expense" for _m, _t, p, _b in mine(self.u, "GET", "transactions")))
