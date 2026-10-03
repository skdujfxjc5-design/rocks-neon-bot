# ROCKS NEON Telegram Bot

A Telegram catalog and payment-review bot starter. It supports Visa/Mastercard product *catalog entries only*, UPI QR requests, a 5-minute payment window, a 9-minute UTR/photo submission window, and manual admin review. It does not collect or issue real card credentials.

## Required environment
- `BOT_TOKEN`: a fresh token from BotFather (keep private)
- `ADMIN_IDS`: comma-separated numeric Telegram user IDs, e.g. `123456789,987654321`
- `DATABASE_PATH`: writable persistent path, e.g. `/app/data/rocks_neon.db` if your host provides a mounted persistent directory; otherwise `rocks_neon.db`
- `CURRENCY`: defaults to `INR`

## Run on a Python worker
Use Python 3.10+ (recommended 3.11), install `requirements.txt`, and set the start command to `python -u bot.py`. Keep exactly one running worker for this bot token because Telegram polling should not be started by multiple instances simultaneously.

## Docker
Build from this directory and run with the environment variables above. Mount a persistent writable volume for the database if orders must survive container replacement. The included Dockerfile uses Python 3.11 and starts the bot directly.

## Payment notes
The QR is a UPI payment request, not proof of payment. Admin must compare the UTR against actual merchant/bank records before approving. Screenshot alone is not verification. The 5-minute QR deadline remains separate from the 9-minute proof-submission deadline. This code does not store PAN, expiry, CVV, PIN, or OTP and does not issue genuine Visa/Mastercard credentials. Use a compliant, authorized payment/card provider for those functions.

## Hosting log 404
A `No such container` / `404` while fetching logs is returned by the hosting platform's container/log service, not by Telegram's bot handler. This package includes standard worker/Docker start files, but only the hosting provider can repair its own missing-container/log service. If it persists, recreate/redeploy the container or contact the host's support and share the platform error.
