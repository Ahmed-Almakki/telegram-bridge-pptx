# telegram-father

## Run the Telegram bot

Run `uv sync` once, then run `make run`. When a user sends a Telegram message,
`main.py` passes its text to `gemini_browser.py` as `initial_prompt` and waits
for the PowerPoint to download into `downloads/`. The bot then sends that file
back to the user.

The Gemini browser must have a signed-in Chrome session available through the
remote debugging setup used by the script. `make browser` runs Gemini directly
with the default prompt for manual use.
