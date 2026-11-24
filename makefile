run:
	uv run python src/telegram_father/main.py

browser:
	uv run python src/telegram_father/gemini_browser.py 

gemini: browser
