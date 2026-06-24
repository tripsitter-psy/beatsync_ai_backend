import os
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

url: str = os.environ.get("SUPABASE_URL", "")
# The backend writes jobs and spends credits on the user's behalf, which RLS
# blocks for the anon key — so prefer the service-role key when present.
key: str = (
    os.environ.get("SUPABASE_SERVICE_KEY", "") 
    or os.environ.get("SUPABASE_SERVICE_ROLE", "") 
    or os.environ.get("SUPABASE_KEY", "")
)

# True only when the service-role key is set (required for credit/job writes).
has_service_key: bool = bool(os.environ.get("SUPABASE_SERVICE_KEY", "")) or bool(os.environ.get("SUPABASE_SERVICE_ROLE", ""))

supabase: Client | None = None
if url and key:
    supabase = create_client(url, key)
    if not has_service_key:
        print("Warning: using the anon Supabase key — set SUPABASE_SERVICE_KEY for backend credit/job writes.")
else:
    print("Warning: SUPABASE_URL/KEY not found. Running on the in-memory mock DB.")
