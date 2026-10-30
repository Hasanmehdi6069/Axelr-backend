"""
Unit tests for the Unified Multi-DB State Engine persistence functionality.
Verifies chat message creation, retrieval, and deletion of guest sessions.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import asyncio
import time
from datetime import datetime, timezone
from core.persistence import PERSIST, _doc_to_message, ChatMessage

async def test_chat_message_persistence():
    """Test that chat messages are saved, retrieved, and can be deleted for guest sessions"""
    # Skip if MongoDB isn't configured (this will work in integration mode)
    if not PERSIST.chats._client:
        print("MongoDB not configured, skipping persistence tests")
        return
    
    # Test parameters
    test_session_id = "test-session-123"
    guest_user_id = "guest_abc123"  # Guest user
    workspace = "core"
    
    # 1. Record an exchange (simulates what happens after an AI response is generated)
    user_msg_id, ai_msg_id = await PERSIST.record_exchange(
        session_id=test_session_id,
        user_id=guest_user_id,
        user_prompt="Hello, this is a test question",
        assistant_reply="This is a test AI response",
        workspace=workspace,
        is_guest=True
    )
    
    print(f"Created test messages: user={user_msg_id}, ai={ai_msg_id}")
    assert user_msg_id is not None
    assert ai_msg_id is not None
    
    # 2. Commit the exchange (marks messages as fully saved)
    await PERSIST.commit_exchange(user_msg_id, ai_msg_id)
    
    # 3. Retrieve the session messages to verify they were saved
    messages = await PERSIST.chats.get_session_messages(test_session_id, guest_user_id)
    print(f"Retrieved {len(messages)} messages for test session")
    assert len(messages) == 2, f"Expected 2 messages, got {len(messages)}"
    
    # Verify message content
    user_msg = next(m for m in messages if m.role == "user")
    ai_msg = next(m for m in messages if m.role == "assistant")
    assert user_msg.content == "Hello, this is a test question"
    assert ai_msg.content == "This is a test AI response"
    print("✓ Message content verification passed")
    
    # 4. Test guest session deletion
    await PERSIST.chats.delete_guest_session(test_session_id, guest_user_id)
    print("✓ Guest session marked for deletion")
    
    # Verify that expire_at was set to now
    updated_docs = await PERSIST.chats._client.find({"session_id": test_session_id}).to_list(length=10)
    for doc in updated_docs:
        assert doc["expire_at"] <= time.time() + 1, "expire_at should be set to now or earlier"
    print("✓ Guest session deletion verification passed")
    
    print("\n✅ All persistence tests passed!")

if __name__ == "__main__":
    asyncio.run(test_chat_message_persistence())