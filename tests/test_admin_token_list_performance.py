import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import orjson

from app.control.account.models import AccountPage, AccountRecord
from app.control.account.refresh import AccountRefreshService, RefreshResult
from app.control.account.backends.local import LocalAccountRepository
from app.control.account.commands import AccountPatch, AccountUpsert
from app.control.account.enums import AccountStatus
from app.products.web.admin import tokens as admin_tokens


class _FastListRepo:
    def __init__(self) -> None:
        self.fast_called = False
        self.list_called = False

    async def list_token_payloads(self) -> list[dict]:
        self.fast_called = True
        return [{
            "token": "tok-1",
            "pool": "basic",
            "status": "active",
            "quota": {},
            "use_count": 0,
            "fail_count": 0,
            "last_used_at": None,
            "tags": [],
        }]

    async def list_accounts(self, query):
        self.list_called = True
        raise AssertionError("list_tokens should use the compact token payload path")


class _FastInvalidRepo:
    def __init__(self) -> None:
        self.fast_called = False
        self.payload_called = False
        self.deleted: list[str] = []

    async def list_confirmed_invalid_tokens(self) -> list[str]:
        self.fast_called = True
        return ["expired-token"]

    async def list_token_payloads(self) -> list[dict]:
        self.payload_called = True
        raise AssertionError("delete_invalid_tokens should use the invalid-token fast path")

    async def delete_accounts(self, tokens: list[str]):
        self.deleted = tokens


class _PagedRepo:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def list_accounts(self, query):
        return AccountPage(
            items=[
                AccountRecord(token="active-token", status=AccountStatus.ACTIVE),
                AccountRecord(
                    token="expired-token",
                    status=AccountStatus.EXPIRED,
                    ext={"invalid_recheck_confirmed_at": 123},
                ),
            ],
            total=2,
            page=1,
            page_size=2000,
            total_pages=1,
        )

    async def delete_accounts(self, tokens: list[str]):
        self.deleted = tokens


class _StubRecheckService(AccountRefreshService):
    def __init__(self, repository, result: RefreshResult) -> None:
        super().__init__(repository)
        self.result = result

    async def _refresh_one(self, record, **kwargs) -> RefreshResult:
        return self.result


class AdminTokenListPerformanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_tokens_uses_compact_payload_fast_path(self):
        repo = _FastListRepo()

        response = await admin_tokens.list_tokens(repo=repo)

        body = orjson.loads(response.body)
        self.assertTrue(repo.fast_called)
        self.assertFalse(repo.list_called)
        self.assertEqual(body["tokens"][0]["token"], "tok-1")

    async def test_local_repository_returns_compact_token_payloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = LocalAccountRepository(Path(tmp) / "accounts.db")
            await repo.initialize()
            await repo.upsert_accounts([
                AccountUpsert(token="tok-1", pool="basic", tags=["nsfw"]),
            ])
            await repo.patch_accounts([
                AccountPatch(
                    token="tok-1",
                    usage_use_delta=3,
                    usage_fail_delta=2,
                    quota_console={"remaining": 4, "total": 5},
                )
            ])

            items = await repo.list_token_payloads()

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["token"], "tok-1")
        self.assertEqual(items[0]["pool"], "basic")
        self.assertEqual(items[0]["status"], "active")
        self.assertEqual(items[0]["use_count"], 3)
        self.assertEqual(items[0]["fail_count"], 2)
        self.assertEqual(items[0]["quota"]["console"], {"remaining": 4, "total": 5})
        self.assertEqual(items[0]["tags"], ["nsfw"])
        self.assertNotIn("ext", items[0])

    async def test_local_repository_tolerates_legacy_blank_quota_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "accounts.db"
            repo = LocalAccountRepository(db_path)
            await repo.initialize()
            await repo.upsert_accounts([AccountUpsert(token="tok-1", pool="basic")])

            import sqlite3

            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    "UPDATE accounts SET quota_auto = '', quota_console = 'not-json'"
                )
                conn.commit()

            items = await repo.list_token_payloads()

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["quota"]["auto"], {"remaining": 0, "total": 0})
        self.assertEqual(items[0]["quota"]["console"], {"remaining": 0, "total": 0})

    async def test_local_repository_only_lists_confirmed_invalid_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = LocalAccountRepository(Path(tmp) / "accounts.db")
            await repo.initialize()
            await repo.upsert_accounts([
                AccountUpsert(token="unconfirmed", pool="basic"),
                AccountUpsert(token="confirmed", pool="basic"),
            ])
            await repo.patch_accounts([
                AccountPatch(token="unconfirmed", status=AccountStatus.EXPIRED),
                AccountPatch(
                    token="confirmed",
                    status=AccountStatus.EXPIRED,
                    ext_merge={
                        "invalid_recheck_count": 2,
                        "invalid_recheck_confirmed_at": 123,
                    },
                ),
            ])

            tokens = await repo.list_confirmed_invalid_tokens()
            payloads = await repo.list_token_payloads()

        self.assertEqual(tokens, ["confirmed"])
        by_token = {item["token"]: item for item in payloads}
        self.assertFalse(by_token["unconfirmed"]["invalid_confirmed"])
        self.assertTrue(by_token["confirmed"]["invalid_confirmed"])
        self.assertEqual(by_token["confirmed"]["invalid_recheck_count"], 2)

    async def test_expired_account_requires_two_failed_rechecks_before_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = LocalAccountRepository(Path(tmp) / "accounts.db")
            await repo.initialize()
            await repo.upsert_accounts([AccountUpsert(token="expired-token")])
            await repo.patch_accounts([
                AccountPatch(token="expired-token", status=AccountStatus.EXPIRED),
            ])
            service = _StubRecheckService(repo, RefreshResult(expired=1))

            first = await service.recheck_expired_token("expired-token")
            self.assertEqual(await repo.list_confirmed_invalid_tokens(), [])
            second = await service.recheck_expired_token("expired-token")

            self.assertEqual(first["outcome"], "invalid")
            self.assertEqual(second["outcome"], "confirmed_invalid")
            self.assertEqual(await repo.list_confirmed_invalid_tokens(), ["expired-token"])

    async def test_successful_recheck_restores_expired_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = LocalAccountRepository(Path(tmp) / "accounts.db")
            await repo.initialize()
            await repo.upsert_accounts([AccountUpsert(token="expired-token")])
            await repo.patch_accounts([
                AccountPatch(
                    token="expired-token",
                    status=AccountStatus.EXPIRED,
                    ext_merge={
                        "invalid_recheck_count": 2,
                        "invalid_recheck_confirmed_at": 123,
                    },
                ),
            ])
            service = _StubRecheckService(repo, RefreshResult(refreshed=1))

            result = await service.recheck_expired_token("expired-token")
            record = (await repo.get_accounts(["expired-token"]))[0]

            self.assertEqual(result["outcome"], "recovered")
            self.assertEqual(record.status, AccountStatus.ACTIVE)
            self.assertNotIn("invalid_recheck_confirmed_at", record.ext)

    async def test_local_repository_initializes_live_updated_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "accounts.db"
            repo = LocalAccountRepository(db_path)
            await repo.initialize()

            import sqlite3

            with closing(sqlite3.connect(db_path)) as conn:
                indexes = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'index'"
                    )
                }

        self.assertIn("idx_acc_live_updated", indexes)

    async def test_local_repository_token_payload_query_uses_live_updated_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "accounts.db"
            repo = LocalAccountRepository(db_path)
            await repo.initialize()

            import sqlite3

            with closing(sqlite3.connect(db_path)) as conn:
                plan = [
                    row[-1]
                    for row in conn.execute(
                        f"EXPLAIN QUERY PLAN {repo._token_payload_select_sql()}"
                    )
                ]

        self.assertTrue(
            any("idx_acc_live_updated" in detail for detail in plan),
            plan,
        )

    async def test_delete_invalid_tokens_uses_invalid_token_fast_path(self):
        repo = _FastInvalidRepo()

        response = await admin_tokens.delete_invalid_tokens(repo=repo)

        body = orjson.loads(response.body)
        self.assertTrue(repo.fast_called)
        self.assertFalse(repo.payload_called)
        self.assertEqual(body["deleted"], 1)
        self.assertEqual(repo.deleted, ["expired-token"])

    async def test_delete_invalid_tokens_fallback_keeps_active_accounts(self):
        repo = _PagedRepo()

        response = await admin_tokens.delete_invalid_tokens(repo=repo)

        body = orjson.loads(response.body)
        self.assertEqual(body["deleted"], 1)
        self.assertEqual(repo.deleted, ["expired-token"])


if __name__ == "__main__":
    unittest.main()
