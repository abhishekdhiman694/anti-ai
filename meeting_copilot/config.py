import os
from dotenv import load_dotenv

load_dotenv()

# The backend server holds the real OpenAI key/prompts - this is the deployed
# Render URL. Override with MEETING_COPILOT_SERVER_URL for local dev/testing
# against a local server instead.
SERVER_URL = os.getenv(
    "MEETING_COPILOT_SERVER_URL", "https://meeting-copilot-backend-yj53.onrender.com"
)

# Audio capture
SAMPLE_RATE = 16000
CHANNELS = 1
SILENCE_RMS_THRESHOLD = 150      # below this = silence (16-bit PCM RMS)
MIN_UTTERANCE_SECONDS = 1.0      # ignore shorter blips
MAX_UTTERANCE_SECONDS = 20.0     # force cut long continuous speech
SILENCE_HANG_SECONDS = 0.4       # silence needed to close an utterance
