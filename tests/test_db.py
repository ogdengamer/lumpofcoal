import threading

from app.db import make_engine


def test_engine_hands_out_a_connection_to_every_worker_thread(tmp_path):
    """40 requests at once (the worker thread limit) each get a connection without waiting.

    With a capped pool the 16th waited 30 seconds, holding a thread the others
    needed to give theirs back, and the site hung while many photos loaded.
    """
    engine = make_engine(str(tmp_path))
    held = []

    def take_all():
        held.extend(engine.connect() for _ in range(40))

    worker = threading.Thread(target=take_all, daemon=True)
    worker.start()
    worker.join(timeout=5)
    try:
        assert not worker.is_alive() and len(held) == 40
    finally:
        for conn in held:
            conn.close()
