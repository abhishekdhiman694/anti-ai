import os
from dotenv import load_dotenv

load_dotenv()

# The backend server holds the real OpenAI key/prompts - update this to your
# deployed Render URL before building the exe you hand out. Left pointing at
# localhost by default for local dev/testing.
SERVER_URL = os.getenv("MEETING_COPILOT_SERVER_URL", "http://127.0.0.1:8000")

# Audio capture
SAMPLE_RATE = 16000
CHANNELS = 1
SILENCE_RMS_THRESHOLD = 150      # below this = silence (16-bit PCM RMS)
MIN_UTTERANCE_SECONDS = 1.0      # ignore shorter blips
MAX_UTTERANCE_SECONDS = 20.0     # force cut long continuous speech
SILENCE_HANG_SECONDS = 0.4       # silence needed to close an utterance
