import os
import json
import asyncpg
from loguru import logger


# Per-flow asyncpg pools, keyed by flow name. Each pooled flow reads its own
# DB_*_{FLOW_UPPER} env vars (and optional DB_POOL_MIN_SIZE_{FLOW_UPPER} /
# DB_POOL_MAX_SIZE_{FLOW_UPPER}). Adding a new pooled flow = add env vars +
# list it in _POOLED_FLOWS. Initialized at FastAPI startup via init_pools()
# and torn down via close_pools() on shutdown.
_pools: dict[str, asyncpg.pool.Pool] = {}
_POOLED_FLOWS = frozenset({"fusion_mfi_explore", "fusion_mfi_settlement", "fusion_mfi_emi", "seed_fincap_emi", "fusion_msme"})


def _resolve_db_env(suffix: str) -> dict:
    """Resolve DB_*_{SUFFIX} env vars for a given flow suffix."""
    return {
        "host": os.environ.get(f"DB_HOST_{suffix}"),
        "port": os.environ.get(f"DB_PORT_{suffix}"),
        "database": os.environ.get(f"DB_NAME_{suffix}"),
        "user": os.environ.get(f"DB_USER_{suffix}"),
        "password": os.environ.get(f"DB_PASSWORD_{suffix}"),
    }


def _resolve_pool_size(suffix: str, key: str, default: int) -> int:
    """Resolve DB_POOL_{KEY}_SIZE_{SUFFIX} env var, falling back to default."""
    raw = os.environ.get(f"DB_POOL_{key}_SIZE_{suffix}")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(f"Invalid DB_POOL_{key}_SIZE_{suffix}={raw!r}; using default {default}")
        return default


async def init_pools() -> None:
    """Create asyncpg pools for every flow in _POOLED_FLOWS. Idempotent.

    Per-flow env vars: DB_HOST/PORT/NAME/USER/PASSWORD_{FLOW_UPPER}.
    Optional sizing: DB_POOL_MIN_SIZE_{FLOW_UPPER}, DB_POOL_MAX_SIZE_{FLOW_UPPER}
    (defaults: 2 / 20). A flow with missing connection env vars is skipped with
    an error log so a misconfigured flow does not block startup of the others.
    """
    for flow in _POOLED_FLOWS:
        if flow in _pools:
            continue
        suffix = flow.upper()
        if suffix == "SEED_FINCAP_EMI":
            suffix = "SEED_FINCAP"
        env = _resolve_db_env(suffix)
        missing = [k for k, v in env.items() if not v]
        if missing:
            logger.error(f"Cannot init pool for '{flow}' — missing env vars: {missing}")
            continue
        min_size = _resolve_pool_size(suffix, "MIN", 2)
        max_size = _resolve_pool_size(suffix, "MAX", 20)
        logger.info(
            f"Initializing asyncpg pool for '{flow}' "
            f"({env['host']}:{env['port']}/{env['database']}, min={min_size}, max={max_size})"
        )
        try:
            _pools[flow] = await asyncpg.create_pool(
                host=env["host"],
                port=env["port"],
                database=env["database"],
                user=env["user"],
                password=env["password"],
                min_size=min_size,
                max_size=max_size,
            )
            logger.info(f"asyncpg pool ready for '{flow}'")
        except Exception as e:
            logger.error(f"Failed to initialize pool for '{flow}': {e}")


async def close_pools() -> None:
    """Close all asyncpg pools. Called from FastAPI shutdown."""
    for flow, pool in list(_pools.items()):
        logger.info(f"Closing asyncpg pool for '{flow}'")
        await pool.close()
    _pools.clear()


async def _fetch_history(conn, account_id: int) -> tuple:
    """Run the two history queries on a given connection.

    Returns: (interaction_history, narrative, account_status, prompt_blocks, detected_language, commitments)
    """
    query_interactions = """
        SELECT combined_json
        FROM public.ai_disposition_analytics
        WHERE account_id = $1
        ORDER BY created_at DESC
        LIMIT 10;
    """
    query_context = """
        SELECT combined_intelligence
        FROM public.ai_account_latest_contexts
        WHERE account_id = $1;
    """

    interaction_history = []
    narrative = "No narrative set."
    account_status = "No status set."
    prompt_blocks = None
    detected_language = None
    commitments = []

    # account_id in ai_disposition_analytics is varchar(100)
    records_interactions = await conn.fetch(query_interactions, str(account_id))
    logger.info(f"Fetched {len(records_interactions)} interactions for account ID {account_id}")

    for record in records_interactions:
        combined_json = record['combined_json']
        if combined_json:
            if isinstance(combined_json, str):
                try:
                    combined_json = json.loads(combined_json)
                except json.JSONDecodeError:
                    combined_json = None
            if isinstance(combined_json, dict):
                interaction_history.append(combined_json)

    # account_id in ai_account_latest_contexts is bigint
    record_context = await conn.fetchrow(query_context, int(account_id))
    if record_context:
        combined_intelligence = record_context['combined_intelligence']
        if isinstance(combined_intelligence, str):
            try:
                combined_intelligence = json.loads(combined_intelligence)
            except json.JSONDecodeError:
                combined_intelligence = {}

        if isinstance(combined_intelligence, dict):
            narrative = combined_intelligence.get('narrative', narrative)
            account_status = combined_intelligence.get('account_status', account_status)
            prompt_blocks = combined_intelligence.get('prompt_blocks')
            detected_language = combined_intelligence.get('language')
            commitments = combined_intelligence.get('commitments') or []

    return interaction_history, narrative, account_status, prompt_blocks, detected_language, commitments


async def get_recent_interactions(account_id: int, flow: str) -> tuple:
    """
    Fetches the latest interaction records for a given account_id,
    as well as the account's narrative, status, and prompt blocks.

    Flows registered in _POOLED_FLOWS use a per-flow asyncpg pool when it has
    been initialized — falls back to per-call connect if the pool is not
    available (e.g. startup-time env was missing). All other flows use a
    per-call asyncpg.connect() against DB_*_{FLOW} env vars
    (e.g. flow="fusion_mfi_settlement" -> DB_HOST_FUSION_MFI_SETTLEMENT, ...).

    Returns: (interaction_history, narrative, account_status, prompt_blocks, detected_language, commitments)
    """
    try:
        pool = _pools.get(flow)
        if pool is not None:
            async with pool.acquire() as conn:
                result = await _fetch_history(conn, account_id)
            logger.info(f"Final history retrieved: {len(result[0])} items for account {account_id}")
            return result

        # Fallback: per-call connect (unpooled flows, or pool init failed)
        suffix = flow.upper()
        if suffix == "SEED_FINCAP_EMI":
            suffix = "SEED_FINCAP"
        env = _resolve_db_env(suffix)
        missing = [f"DB_*_{suffix}".replace("*", k.upper()) for k, v in env.items() if not v]
        if missing:
            logger.error(f"Missing env vars for flow '{flow}': {missing}. Cannot fetch history.")
            return [], "No narrative.", "No status.", None, None, []

        logger.info(f"Connecting to DB: {env['host']}:{env['port']}/{env['database']} as {env['user']}...")
        conn = await asyncpg.connect(
            user=env["user"],
            password=env["password"],
            database=env["database"],
            host=env["host"],
            port=env["port"],
        )
        try:
            result = await _fetch_history(conn, account_id)
        finally:
            await conn.close()
        logger.info(f"Final history retrieved: {len(result[0])} items for account {account_id}")
        return result

    except Exception as e:
        logger.error(f"Failed to retrieve interaction history for account ID {account_id}: {e}")
        return [], "Error retrieving narrative.", "Error retrieving status.", None, None, []
