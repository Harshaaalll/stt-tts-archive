import os
import json
import asyncpg
from loguru import logger


async def get_recent_interactions(account_id: int) -> tuple:
    """
    Fetches the latest 5 interaction records for a given account_id,
    as well as the account's narrative, status, and pre-built prompt_blocks.
    Returns: (interaction_history, narrative, account_status, prompt_blocks)
    prompt_blocks is a dict {block_name: {version, addendum}} or None if not yet built.
    """
    db_host = os.environ.get("DB_HOST", "localhost")
    db_port = os.environ.get("DB_PORT", "5432")
    db_name = os.environ.get("DB_NAME", "rag_test_db")
    db_user = os.environ.get("DB_USER", "postgres")
    db_pass = os.environ.get("DB_PASSWORD", "123456789")
    
    if not all([db_host, db_name, db_user]):
        logger.warning("Database configuration missing. Skipping history retrieval.")
        return [], "No narrative.", "No status.", None  # prompt_blocks=None

    # Fetch history joined with transcript logs.
    # LEFT JOIN ensures the pending task (no transcript yet) is included for narrative/prompt_blocks.
    query = """
        SELECT
            at.id as activity_id,
            at.status,
            at.disposition,
            at.summary,
            at.sentiment,
            at.created as activity_date,
            at.ptp as ptp_date,
            at.prompt_blocks,
            at.narrative,
            at.account_status,
            ag.response,
            ag.id as transcript_id
        FROM activity_taskactivity at
        LEFT JOIN activity_genatranscriptlogs ag ON ag.activity_id = at.id
        WHERE at.account_id = $1
        ORDER BY at.created DESC NULLS LAST;
    """

    interaction_history = []
    prompt_blocks = None
    
    try:
        conn = await asyncpg.connect(
            user=db_user, password=db_pass, database=db_name, host=db_host, port=db_port
        )
        try:
            records = await conn.fetch(query, account_id)
            
            if records:
                # 1. Extract narrative/status from the first record that has them 
                # (Skipping placeholder or dialer-created records that might be empty)
                narrative = "No narrative set."
                account_status = "No status set."
                
                # Invalid indicators to skip when looking for the primary narrative
                invalid_n = ["No narrative set.", "No narrative.", "Error retrieving narrative.", "__NO_HISTORY__"]
                
                for record in records:
                    r_narrative = record.get('narrative')
                    if r_narrative and r_narrative.strip() and not any(ind in r_narrative for ind in invalid_n):
                        narrative = r_narrative
                        account_status = record.get('account_status') or "No status set."
                        logger.info(f"History Retriever: Found valid narrative in record ID {record['activity_id']}")
                        break
                
                # Fallback to the latest record if no valid narrative found anywhere
                if narrative == "No narrative set." and records:
                    narrative = records[0]['narrative'] or "No narrative set."
                    account_status = records[0]['account_status'] or "No status set."
                
                # 2. Find the latest prompt_blocks (from the most recent record that has them)
                for record in records:
                    if record['prompt_blocks']:
                        try:
                            if isinstance(record['prompt_blocks'], str):
                                prompt_blocks = json.loads(record['prompt_blocks'])
                            else:
                                prompt_blocks = record['prompt_blocks']
                            break
                        except Exception:
                            continue

                # 3. Build interaction history (ONLY from records with transcript logs)
                # We filter for records where transcript_id is NOT NULL
                ai_interactions = [r for r in records if r['transcript_id'] is not None]
                
                for record in ai_interactions[:10]:
                    response_data = record['response']
                    if response_data:
                        if isinstance(response_data, str):
                            try:
                                response_data = json.loads(response_data)
                            except json.JSONDecodeError:
                                response_data = None
                        if isinstance(response_data, dict):
                            interaction_history.append(response_data)
                            continue

                # Ensure history is strictly ordered: Latest First, Oldest Last
                interaction_history.sort(key=lambda x: x.get('date', ''), reverse=True)

                return interaction_history, narrative, account_status, prompt_blocks

            return [], "No narrative set.", "No status set.", None  # prompt_blocks=None

        finally:
            await conn.close()
    except Exception as e:
        logger.error(f"Failed to retrieve interaction history for account ID {account_id}: {e}")
        return [], "Error retrieving narrative.", "Error retrieving status.", None  # prompt_blocks=None
