# test_vectorize.py
"""
Test script to verify Cloudflare Vectorize is working correctly.
Run this after setting up your Vectorize index and environment variables.
"""

import asyncio
import os

from dotenv import load_dotenv

# Load environment variables
load_dotenv()

async def main():
    print("🧪 Testing Cloudflare Vectorize integration...")
    
    # Check if environment variables are set
    required_vars = ["CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_VECTORIZE_INDEX"]
    missing = [var for var in required_vars if not os.getenv(var)]
    
    if missing:
        print(f"❌ Missing environment variables: {', '.join(missing)}")
        print("\nPlease add these to your .env file:")
        print("CLOUDFLARE_ACCOUNT_ID=your_account_id")
        print("CLOUDFLARE_API_TOKEN=your_api_token") 
        print("CLOUDFLARE_VECTORIZE_INDEX=axelr-vectors")
        return
    
    print("✅ All environment variables found")
    
    # Test the Vectorize cache
    from core.cloudflare_vectorize import get_vector_cache
    
    try:
        cache = get_vector_cache()
        print("✅ Vectorize cache initialized successfully")
        
        # Test model loading
        print("\n📥 Loading embedding model...")
        await cache._ensure_model()
        if cache._model_loaded:
            print("✅ Embedding model loaded successfully")
        else:
            print("❌ Failed to load embedding model")
            return
            
        # Test storing a vector
        print("\n📤 Testing vector insert...")
        test_prompt = "What is the capital of France?"
        test_response = "The capital of France is Paris."
        await cache.set(test_prompt, test_response)
        print("✅ Vector inserted successfully")
        
        # Test retrieving the vector
        print("\n🔍 Testing vector query...")
        similar_prompt = "Can you tell me the capital city of France?"
        result = await cache.get(similar_prompt)
        
        if result:
            print(f"✅ Cache hit! Got response: {result}")
        else:
            print("⚠️ Cache miss - this might be normal if the vector hasn't propagated yet")
            
        # Print stats
        print(f"\n📊 Cache stats: {cache.stats()}")
        print("\n🎉 Cloudflare Vectorize setup test completed!")
        
        # Close the session to prevent unclosed client warnings
        await cache.close()
        
    except Exception as e:
        print(f"❌ Error during testing: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())