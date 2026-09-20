import os
import subprocess
import sys
import urllib.parse


# --- Installation ---
def install(package):
    """Installs a package using pip."""
    try:
        print(f"Installing {package}...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", package])
        print(f"{package} installed successfully.")
    except subprocess.CalledProcessError as e:
        print(f"Failed to install {package}. Error: {e}")
        sys.exit(1)

# --- Check and Install Dependencies ---
try:
    from dotenv import load_dotenv
except ImportError:
    install("python-dotenv")
    from dotenv import load_dotenv

try:
    import pg8000.dbapi
except ImportError:
    install("pg8000")
    import pg8000.dbapi

try:
    from supabase import Client, create_client
except ImportError:
    install("supabase")
    from supabase import Client, create_client

# --- Connection Tests ---
load_dotenv()

def test_neon_connection():
    """Tests the connection to the Neon database using pg8000."""
    print("\n--- Testing Neon Connection (using pg8000) ---")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("❌ Neon connection failed: DATABASE_URL not found in .env file.")
        return

    try:
        print("Connecting to Neon...")
        result = urllib.parse.urlparse(database_url)
        conn = pg8000.dbapi.connect(
            user=result.username,
            password=result.password,
            host=result.hostname,
            # Correctly use the port from the URL or default to 5432
            port=result.port or 5432,
            database=result.path[1:],
            ssl_context=True
        )
        print("✅ Neon connection successful!")
        conn.close()
    except Exception as e:
        print(f"❌ Neon connection failed: {e}")

def test_supabase_connection():
    """Tests the connection to the Supabase service."""
    print("\n--- Testing Supabase Connection ---")
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SECRET_API_KEY")

    if not url or not key:
        print("❌ Supabase connection failed: SUPABASE_URL or SUPABASE_SECRET_API_KEY not found in .env file.")
        return

    try:
        print("Connecting to Supabase...")
        supabase: Client = create_client(url, key)
        supabase.storage.list_buckets()
        print("✅ Supabase connection successful!")
    except Exception as e:
        print(f"❌ Supabase connection failed: {e}")

if __name__ == "__main__":
    print("Starting connection tests...")
    test_neon_connection()
    test_supabase_connection()
    print("\nTests complete.")