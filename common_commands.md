DBMATE (database)

create migration
uv run --env-file .env.dbmate dbmate new add_voice_calls

apply migration
dbmate --env-file .env.dbmate up
dbmate --env-file .env.dbmate --env TEST_DATABASE_URL up


run fast api with app file specified

uv run uvicorn app.main:create_app \
  --host 127.0.0.1 \
  --port 8000 \
  --reload


RESTAURANT_ENVIRONMENT=local uv run uvicorn \
  app.api.voice:create_voice_demo_app \
  --factory \
  --host 127.0.0.1 \
  --port 8001 \
  --reload

with websockets 

uv run uvicorn app.main:create_app \
  --factory \
  --host 127.0.0.1 \
  --port 8000 \
  --reload \
  --ws websockets-sansio \
  --ws-max-size 16384 \
  --ws-max-queue 16



run tests

uv run python -m pytest

uv run --env-file .env.dbmate pytest -q

uv run python -m pytest tests/test_voice.py -q






Expose port 8001 through an HTTPS tunnel.

Create or sign in to an ngrok account. On your Mac, install its command-line tool:
brew install ngrok

Copy your token from the ngrok authtoken page, then replace the placeholder locally:
ngrok config add-authtoken YOUR_NGROK_AUTHTOKEN

Start the tunnel in terminal 2:
ngrok http 8001

These are ngrok’s documented installation and connection commands. The agent provides a public HTTPS URL forwarding to your local application. ngrok documentation

Copy the HTTPS forwarding address displayed by ngrok. Your webhook URL is that address plus:
/twilio/voice

Test the public endpoint, replacing the hostname below:
curl -i -X POST \
  "https://YOUR-NGROK-DOMAIN/twilio/voice"

Expect the same XML response as the local request. Keep both Uvicorn and ngrok running.

ngrok http 8000 --url https://untimely-motor-avid.ngrok-free.dev --traffic-policy-file ngrok-voice.yml



Address already in use

lsof -nP -iTCP:8000 -sTCP:LISTEN 

kill -9 pid

