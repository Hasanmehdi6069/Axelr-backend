
import os
import requests
from dotenv import load_dotenv

load_dotenv()

ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID")
INDEX_NAME = os.getenv("CLOUDFLARE_VECTORIZE_INDEX")
API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN")

if not all([ACCOUNT_ID, INDEX_NAME, API_TOKEN]):
    print("Cloudflare environment variables not set.")
else:
    headers = {"Authorization": f"Bearer {API_TOKEN}"}
    
    # Upsert a test vector
    import numpy as np

# ... (rest of the imports)

upsert_data = {
    "ids": ["test-id"],
    "vectors": [np.random.rand(384).tolist()]
}
    upsert_url = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/vectorize/indexes/{INDEX_NAME}/upsert"
    
    try:
        upsert_response = requests.post(upsert_url, headers=headers, json=upsert_data)
        upsert_response.raise_for_status()
        print("Cloudflare Vectorize: Upsert successful.")
        
        # Query the test vector
        query_data = {
    "vector": upsert_data["vectors"][0],
    "topK": 1
}
        query_url = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/vectorize/indexes/{INDEX_NAME}/query"
        
        query_response = requests.post(query_url, headers=headers, json=query_data)
        query_response.raise_for_status()
        
        response_json = query_response.json()
        if response_json.get("matches") and response_json["matches"][0]["id"] == "test-id":
            print("Cloudflare Vectorize: Query successful and returned the correct vector.")
        else:
            print("Cloudflare Vectorize: Query failed or returned incorrect data.")
            
    except requests.exceptions.RequestException as e:
        print(f"Cloudflare Vectorize: An error occurred: {e}")
        if e.response:
            print(f"Response content: {e.response.text}")