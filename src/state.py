import asyncio
class AppState:
    def __init__(self):
        self.shutdown_event = asyncio.Event()
        self.loop = asyncio.get_event_loop()

state = AppState()
