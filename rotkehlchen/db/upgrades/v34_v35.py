from typing import TYPE_CHECKING, List, Tuple

from rotkehlchen.constants.resolver import (
    ETHEREUM_DIRECTIVE,
    ETHEREUM_DIRECTIVE_LENGTH,
    ChainID,
    evm_address_to_identifier,
)
from rotkehlchen.globaldb.upgrades.v2_v3 import OTHER_EVM_CHAINS_ASSETS
from rotkehlchen.types import EvmTokenKind

if TYPE_CHECKING:
    from rotkehlchen.db.dbhandler import DBHandler
    from rotkehlchen.db.drivers.gevent import DBConnection, DBCursor


def _refactor_time_columns(write_cursor: 'DBCursor') -> None:
    """
    The tables that contained time instead of timestamp as column names and need
    to be changed were:
    - timed_balances
    - timed_location_data
    - ethereum_accounts_details
    - trades
    - asset_movements
    """
    write_cursor.execute('ALTER TABLE timed_balances RENAME COLUMN time TO timestamp')
    write_cursor.execute('ALTER TABLE timed_location_data RENAME COLUMN time TO timestamp')
    write_cursor.execute('ALTER TABLE ethereum_accounts_details RENAME COLUMN time TO timestamp')
    write_cursor.execute('ALTER TABLE trades RENAME COLUMN time TO timestamp')
    write_cursor.execute('ALTER TABLE asset_movements RENAME COLUMN time TO timestamp')


def _create_new_tables(write_cursor: 'DBCursor') -> None:
    write_cursor.execute("""
    CREATE TABLE IF NOT EXISTS user_notes(
        identifier INTEGER NOT NULL PRIMARY KEY,
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        location TEXT NOT NULL,
        last_update_timestamp INTEGER NOT NULL,
        is_pinned INTEGER NOT NULL CHECK (is_pinned IN (0, 1))
    );
    """)


def _rename_assets_identifiers(write_cursor: 'DBCursor') -> None:
    """Version 1.26 includes the migration for the global db and the references to assets
    need to be updated also in this database.
    We need to update the rows instead of just deleting and inserting because the rows are
    referenced and this triggers an update in cascade.
    """
    write_cursor.execute('SELECT identifier FROM assets')
    old_id_to_new = {}
    for (identifier,) in write_cursor:
        if identifier.startswith(ETHEREUM_DIRECTIVE):
            old_id_to_new[identifier] = evm_address_to_identifier(
                address=identifier[ETHEREUM_DIRECTIVE_LENGTH:],
                chain=ChainID.ETHEREUM,
                token_type=EvmTokenKind.ERC20,
            )
        elif identifier in OTHER_EVM_CHAINS_ASSETS:
            old_id_to_new[identifier] = OTHER_EVM_CHAINS_ASSETS[identifier]
    sqlite_tuples = [(new_id, old_id) for old_id, new_id in old_id_to_new.items()]
    # Make sure that the new ids don't exist already in the user db. This is the first version
    # that allows the version 3 of the schema so we need to make sure that all the assets
    # comply with the correct format. I (yabirgb) added this DELETE from just for safety
    # but this shouldn't happen during the migration since the new ids shouldn't exist in the
    # database before this upgrade as stated by Alexey in this line
    # https://github.com/rotki/rotki/blob/e68665fcb4f8a6455bab16595a8db75c7faa8b56/rotkehlchen/globaldb/handler.py#L66  # noqa: E501
    write_cursor.executemany('DELETE FROM assets WHERE identifier=?', [(x,) for x in old_id_to_new.values()])  # noqa: E501
    write_cursor.executemany('UPDATE OR IGNORE assets SET identifier=? WHERE identifier=?', sqlite_tuples)  # noqa: E501


def _change_xpub_mappings_primary_key(write_cursor: 'DBCursor', conn: 'DBConnection') -> None:
    """This upgrade includes xpub_mappings' `blockchain` column in primary key.
    After this upgrade it will become possible to create mapping for the same bitcoin address
    and xpub on different blockchains.

    Despite `blockchain` was not previously in the primary key, data in this table should not
    be broken since it has FOREIGN KEY (which includes `blockchain`) referencing xpubs table.
    """
    with conn.read_ctx() as read_cursor:
        xpub_mappings = read_cursor.execute('SELECT * from xpub_mappings').fetchall()
    write_cursor.execute("""CREATE TABLE xpub_mappings_copy (
        address TEXT NOT NULL,
        xpub TEXT NOT NULL,
        derivation_path TEXT NOT NULL,
        account_index INTEGER,
        derived_index INTEGER,
        blockchain TEXT NOT NULL,
        FOREIGN KEY(blockchain, address)
        REFERENCES blockchain_accounts(blockchain, account) ON DELETE CASCADE
        FOREIGN KEY(xpub, derivation_path, blockchain) REFERENCES xpubs(
            xpub,
            derivation_path,
            blockchain
        ) ON DELETE CASCADE
        PRIMARY KEY (address, xpub, derivation_path, blockchain)
    );
    """)
    write_cursor.executemany('INSERT INTO xpub_mappings_copy VALUES (?, ?, ?, ?, ?, ?)', xpub_mappings)  # noqa: E501
    write_cursor.execute('DROP TABLE xpub_mappings')
    write_cursor.execute('ALTER TABLE xpub_mappings_copy RENAME TO xpub_mappings')


def _clean_amm_swaps(cursor: 'DBCursor') -> None:
    """Since we remove the amm swaps, clean all related DB tables and entries"""
    cursor.execute('DELETE FROM used_query_ranges WHERE name LIKE "uniswap_trades%";')
    cursor.execute('DELETE FROM used_query_ranges WHERE name LIKE "sushiswap_trades%";')
    cursor.execute('DELETE FROM used_query_ranges WHERE name LIKE "balancer_trades%";')
    cursor.execute('DROP VIEW IF EXISTS combined_trades_view;')
    cursor.execute('DROP TABLE IF EXISTS amm_swaps;')


def _add_blockchain_column_web3_nodes(cursor: 'DBCursor') -> None:
    cursor.execute('ALTER TABLE web3_nodes RENAME TO web3_nodes_old')
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS web3_nodes(
        identifier INTEGER NOT NULL PRIMARY KEY,
        name TEXT NOT NULL,
        endpoint TEXT NOT NULL,
        owned INTEGER NOT NULL CHECK (owned IN (0, 1)),
        active INTEGER NOT NULL CHECK (active IN (0, 1)),
        weight INTEGER NOT NULL,
        blockchain TEXT NOT NULL
    );
    """)
    cursor.execute("INSERT INTO web3_nodes SELECT identifier, name, endpoint, owned, active, weight, 'ETH' FROM web3_nodes_old")  # noqa: E501
    cursor.execute('DROP TABLE web3_nodes_old')


def _update_ignored_assets_identifiers_to_caip_format(cursor: 'DBCursor') -> None:
    cursor.execute('SELECT value FROM multisettings WHERE name="ignored_asset";')
    old_ids_to_caip_ids_mappings: List[Tuple[str, str]] = []
    for (old_identifier,) in cursor:
        if old_identifier is not None and old_identifier.startswith(ETHEREUM_DIRECTIVE):
            old_ids_to_caip_ids_mappings.append(
                (
                    evm_address_to_identifier(
                        address=old_identifier[ETHEREUM_DIRECTIVE_LENGTH:],
                        chain=ChainID.ETHEREUM,
                        token_type=EvmTokenKind.ERC20,
                    ),
                    old_identifier,
                ),
            )

    cursor.executemany(
        'UPDATE multisettings SET value=? WHERE value=? AND name="ignored_asset"',
        old_ids_to_caip_ids_mappings,
    )


def _rename_assets_in_user_queried_tokens(cursor: 'DBCursor') -> None:
    """ethereum_accounts_details has the column tokens_list as a json list with identifiers
    using the _ceth_ formath. Those need to be upgraded to the CAIPS format.
    """
    cursor.execute('SELECT account, tokens_list FROM ethereum_accounts_details')
    update_rows = []
    for address, token_list in cursor:
        tokens = json.loads(token_list)
        new_ids = []
        for token in tokens.get('tokens', []):
            new_ids.append(
                evm_address_to_identifier(
                    address=token[ETHEREUM_DIRECTIVE_LENGTH:],
                    chain=ChainID.ETHEREUM,
                    token_type=EvmTokenKind.ERC20,
                ),
            )
        update_rows.append((json.dumps({'tokens': new_ids}), address))
    cursor.executemany(
        'UPDATE ethereum_accounts_details SET tokens_list=? WHERE account=?',
        update_rows,
    )


def upgrade_v34_to_v35(db: 'DBHandler') -> None:
    """Upgrades the DB from v34 to v35
    - Change tables where time is used as column name to timestamp
    - Add user_notes table
    - Renames the asset identifiers to use CAIPS
    """
    with db.user_write() as write_cursor:
        _rename_assets_identifiers(write_cursor)
        _update_ignored_assets_identifiers_to_caip_format(write_cursor)
        _refactor_time_columns(write_cursor)
        _clean_amm_swaps(write_cursor)
        _create_new_tables(write_cursor)
        _change_xpub_mappings_primary_key(write_cursor=write_cursor, conn=db.conn)
        _add_blockchain_column_web3_nodes(write_cursor)
        _create_new_tables(write_cursor)
        _rename_assets_in_user_queried_tokens(write_cursor)