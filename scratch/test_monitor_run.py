import sys
import asyncio
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app

class MockBot:
    async def send_message(self, chat_id, text, parse_mode=None):
        print("\n=== MOCK TELEGRAM NOTIFICATION SENT ===")
        print(text)
        print("========================================\n")
        return None

async def main():
    sys.stdout.reconfigure(encoding='utf-8')
    
    # Initialize monitor state
    monitor.load_chat_id()
    monitor.load_seen_ids()
    
    print("seen_ids loaded count:", len(monitor.seen_ids))
    print("Is 68759785 in seen_ids?", "68759785" in monitor.seen_ids)
    print("Chat ID:", monitor.chat_id)
    
    # We will pass a mock bot so it prints instead of using Telegram API
    bot_mock = MockBot()
    
    print("\nStarting process_offers simulation...")
    sent = await monitor.process_offers(
        bot_instance=bot_mock,
        skip_seen=True,
        candidate_limit=10
    )
    print(f"Simulation completed. Sent offers count: {sent}")

if __name__ == '__main__':
    asyncio.run(main())
