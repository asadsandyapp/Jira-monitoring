"""Open the dashboard. The first screen asks which sprint to load.

    python -m monitor
"""
import threading
import webbrowser

from .web import create_app

PORT = 8000


def main():
    url = f"http://127.0.0.1:{PORT}"
    print(f"Dashboard: {url}")
    print("Pick a sprint in the browser. Stop with Ctrl+C")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    create_app().run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
