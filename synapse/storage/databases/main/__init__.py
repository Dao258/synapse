# Copyright 2014-2016 OpenMarket Ltd
# Copyright 2018 New Vector Ltd
# Copyright 2019-2021 The Matrix.org Foundation C.I.C.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
from typing import TYPE_CHECKING, List, Optional, Tuple, Union, cast

from synapse.api.constants import Direction
from synapse.config.homeserver import HomeServerConfig
from synapse.storage._base import make_in_list_sql_clause
from synapse.storage.database import (
    DatabasePool,
    LoggingDatabaseConnection,
    LoggingTransaction,
)
from synapse.storage.databases.main.stats import UserSortOrder
from synapse.storage.engines import BaseDatabaseEngine
from synapse.storage.types import Cursor
from synapse.types import JsonDict, get_domain_from_id

from .account_data import AccountDataStore
from .appservice import ApplicationServiceStore, ApplicationServiceTransactionStore
from .cache import CacheInvalidationWorkerStore
from .censor_events import CensorEventsStore
from .client_ips import ClientIpWorkerStore
from .deviceinbox import DeviceInboxStore
from .devices import DeviceStore
from .directory import DirectoryStore
from .e2e_room_keys import EndToEndRoomKeyStore
from .end_to_end_keys import EndToEndKeyStore
from .event_federation import EventFederationStore
from .event_push_actions import EventPushActionsStore
from .events_bg_updates import EventsBackgroundUpdatesStore
from .events_forward_extremities import EventForwardExtremitiesStore
from .experimental_features import ExperimentalFeaturesStore
from .filtering import FilteringWorkerStore
from .keys import KeyStore
from .lock import LockStore
from .media_repository import MediaRepositoryStore
from .metrics import ServerMetricsStore
from .monthly_active_users import MonthlyActiveUsersWorkerStore
from .openid import OpenIdStore
from .presence import PresenceStore
from .profile import ProfileStore
from .purge_events import PurgeEventsStore
from .push_rule import PushRuleStore
from .pusher import PusherStore
from .receipts import ReceiptsStore
from .registration import RegistrationStore
from .rejections import RejectionsStore
from .relations import RelationsStore
from .room import RoomStore
from .roommember import RoomMemberStore
from .search import SearchStore
from .session import SessionStore
from .signatures import SignatureStore
from .state import StateStore
from .stats import StatsStore
from .stream import StreamWorkerStore
from .tags import TagsStore
from .transactions import TransactionWorkerStore
from .ui_auth import UIAuthStore
from .user_directory import UserDirectoryStore
from .user_erasure_store import UserErasureStore

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class DataStore(
    EventsBackgroundUpdatesStore,
    ExperimentalFeaturesStore,
    DeviceStore,
    RoomMemberStore,
    RoomStore,
    RegistrationStore,
    ProfileStore,
    PresenceStore,
    TransactionWorkerStore,
    DirectoryStore,
    KeyStore,
    StateStore,
    SignatureStore,
    ApplicationServiceStore,
    PurgeEventsStore,
    EventFederationStore,
    MediaRepositoryStore,
    RejectionsStore,
    FilteringWorkerStore,
    PusherStore,
    PushRuleStore,
    ApplicationServiceTransactionStore,
    EventPushActionsStore,
    ServerMetricsStore,
    ReceiptsStore,
    EndToEndKeyStore,
    EndToEndRoomKeyStore,
    SearchStore,
    TagsStore,
    AccountDataStore,
    StreamWorkerStore,
    OpenIdStore,
    ClientIpWorkerStore,
    DeviceInboxStore,
    UserDirectoryStore,
    UserErasureStore,
    MonthlyActiveUsersWorkerStore,
    StatsStore,
    RelationsStore,
    CensorEventsStore,
    UIAuthStore,
    EventForwardExtremitiesStore,
    CacheInvalidationWorkerStore,
    LockStore,
    SessionStore,
):
    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        self.hs = hs
        self._clock = hs.get_clock()
        self.database_engine = database.engine

        super().__init__(database, db_conn, hs)

    async def get_users(self) -> List[JsonDict]:
        """Function to retrieve a list of users in users table.

        Returns:
            A list of dictionaries representing users.
        """
        return await self.db_pool.simple_select_list(
            table="users",
            keyvalues={},
            retcols=[
                "name",
                "password_hash",
                "is_guest",
                "admin",
                "user_type",
                "deactivated",
            ],
            desc="get_users",
        )

    async def get_users_paginate(
        self,
        start: int,
        limit: int,
        user_id: Optional[str] = None,
        name: Optional[str] = None,
        guests: bool = True,
        deactivated: bool = False,
        order_by: str = UserSortOrder.NAME.value,
        direction: Direction = Direction.FORWARDS,
        approved: bool = True,
        not_user_types: Optional[List[str]] = None,
    ) -> Tuple[List[JsonDict], int]:
        """Function to retrieve a paginated list of users from
        users list. This will return a json list of users and the
        total number of users matching the filter criteria.

        Args:
            start: start number to begin the query from
            limit: number of rows to retrieve
            user_id: search for user_id. ignored if name is not None
            name: search for local part of user_id or display name
            guests: whether to in include guest users
            deactivated: whether to include deactivated users
            order_by: the sort order of the returned list
            direction: sort ascending or descending
            approved: whether to include approved users
            not_user_types: list of user types to exclude
        Returns:
            A tuple of a list of mappings from user to information and a count of total users.
        """

        def get_users_paginate_txn(
            txn: LoggingTransaction,
        ) -> Tuple[List[JsonDict], int]:
            filters = []
            args: List[Union[str, int]] = []

            # Set ordering
            order_by_column = UserSortOrder(order_by).value

            if direction == Direction.BACKWARDS:
                order = "DESC"
            else:
                order = "ASC"

            # `name` is in database already in lower case
            if name:
                filters.append("(name LIKE ? OR LOWER(displayname) LIKE ?)")
                args.extend(["@%" + name.lower() + "%:%", "%" + name.lower() + "%"])
            elif user_id:
                filters.append("name LIKE ?")
                args.extend(["%" + user_id.lower() + "%"])

            if not guests:
                filters.append("is_guest = 0")

            if not deactivated:
                filters.append("deactivated = 0")

            if not approved:
                # We ignore NULL values for the approved flag because these should only
                # be already existing users that we consider as already approved.
                filters.append("approved IS FALSE")

            if not_user_types:
                if len(not_user_types) == 1 and not_user_types[0] == "":
                    # Only exclude NULL type users
                    filters.append("user_type IS NOT NULL")
                else:
                    not_user_types_has_empty = False
                    not_user_types_without_empty = []

                    for not_user_type in not_user_types:
                        if not_user_type == "":
                            not_user_types_has_empty = True
                        else:
                            not_user_types_without_empty.append(not_user_type)

                    not_user_type_clause, not_user_type_args = make_in_list_sql_clause(
                        self.database_engine,
                        "u.user_type",
                        not_user_types_without_empty,
                    )

                    if not_user_types_has_empty:
                        # NULL values should be excluded.
                        # They evaluate to false > nothing to do here.
                        filters.append("NOT %s" % (not_user_type_clause))
                    else:
                        # NULL values should *not* be excluded.
                        # Add a special predicate to the query.
                        filters.append(
                            "(NOT %s OR %s IS NULL)"
                            % (not_user_type_clause, "u.user_type")
                        )

                    args.extend(not_user_type_args)

            where_clause = "WHERE " + " AND ".join(filters) if len(filters) > 0 else ""

            sql_base = f"""
                FROM users as u
                LEFT JOIN profiles AS p ON u.name = p.full_user_id
                LEFT JOIN erased_users AS eu ON u.name = eu.user_id
                {where_clause}
                """
            sql = "SELECT COUNT(*) as total_users " + sql_base
            txn.execute(sql, args)
            count = cast(Tuple[int], txn.fetchone())[0]

            sql = f"""
                SELECT name, user_type, is_guest, admin, deactivated, shadow_banned,
                displayname, avatar_url, creation_ts * 1000 as creation_ts, approved,
                eu.user_id is not null as erased
                {sql_base}
                ORDER BY {order_by_column} {order}, u.name ASC
                LIMIT ? OFFSET ?
            """
            args += [limit, start]
            txn.execute(sql, args)
            users = self.db_pool.cursor_to_dict(txn)

            # some of those boolean values are returned as integers when we're on SQLite
            columns_to_boolify = ["erased"]
            for user in users:
                for column in columns_to_boolify:
                    user[column] = bool(user[column])

            return users, count

        return await self.db_pool.runInteraction(
            "get_users_paginate_txn", get_users_paginate_txn
        )

    async def search_users(self, term: str) -> Optional[List[JsonDict]]:
        """Function to search users list for one or more users with
        the matched term.

        Args:
            term: search term

        Returns:
            A list of dictionaries or None.
        """
        return await self.db_pool.simple_search_list(
            table="users",
            term=term,
            col="name",
            retcols=["name", "password_hash", "is_guest", "admin", "user_type"],
            desc="search_users",
        )


def check_database_before_upgrade(
    cur: Cursor, database_engine: BaseDatabaseEngine, config: HomeServerConfig
) -> None:
    """Called before upgrading an existing database to check that it is broadly sane
    compared with the configuration.
    """
    logger.info("Checking database for consistency with configuration...")

    # if there are any users in the database, check that the username matches our
    # configured server name.

    cur.execute("SELECT name FROM users LIMIT 1")
    rows = cur.fetchall()
    if not rows:
        return

    user_domain = get_domain_from_id(rows[0][0])
    if user_domain == config.server.server_name:
        return

    raise Exception(
        "Found users in database not native to %s!\n"
        "You cannot change a synapse server_name after it's been configured"
        % (config.server.server_name,)
    )


__all__ = ["DataStore", "check_database_before_upgrade"]
