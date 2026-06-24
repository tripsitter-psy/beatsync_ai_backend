"""
Credits + account helpers backed by Supabase.

Until Supabase is configured with the service-role key (SUPABASE_SERVICE_KEY)
and the migration is applied, `enabled()` is False and the app runs "open":
generation isn't gated and the account looks like an unlimited dev account. Once
configured, real credit checks and the 2-month free trial kick in.
"""
import logging
from app.supabase_client import supabase, has_service_key

logger = logging.getLogger(__name__)

# 1 credit = 1 generated clip. Beat-sync montages are free.
GENERATE_COST = 1


def enabled() -> bool:
    return supabase is not None and has_service_key


def consume_credit(user_id: str) -> bool:
    """Atomically spend one credit (and enforce the free-trial window) via the
    consume_credit() SQL function. Returns True if allowed to proceed."""
    if not enabled():
        return True  # dev / mock mode: don't gate generation
    try:
        res = supabase.rpc("consume_credit", {"p_user_id": user_id}).execute()
        return bool(res.data)
    except Exception as e:
        logger.error(f"consume_credit failed for {user_id}: {e}")
        # Fail open so a transient DB error doesn't block a paying user.
        return True


def refund_credit(user_id: str) -> None:
    """Give a credit back (e.g. the render failed before doing real work)."""
    if not enabled():
        return
    try:
        supabase.rpc("refund_credit", {"p_user_id": user_id}).execute()
    except Exception as e:
        logger.error(f"refund_credit failed for {user_id}: {e}")


def get_account(user_id: str) -> dict:
    """Account snapshot for the profile UI: credits, tier, trial state."""
    if not enabled():
        return {
            "credits_balance": None,   # null => unlimited/dev
            "subscription_tier": "dev",
            "trial_ends_at": None,
            "trial_active": True,
        }
    try:
        res = (
            supabase.table("users")
            .select("credits_balance, subscription_tier, trial_ends_at")
            .eq("id", user_id)
            .single()
            .execute()
        )
        row = res.data or {}
        return {
            "credits_balance": row.get("credits_balance", 0),
            "subscription_tier": row.get("subscription_tier", "free"),
            "trial_ends_at": row.get("trial_ends_at"),
            "trial_active": True,  # the SQL enforces the real window on spend
        }
    except Exception as e:
        logger.error(f"get_account failed for {user_id}: {e}")
        return {"credits_balance": 0, "subscription_tier": "free", "trial_ends_at": None, "trial_active": False}
