
# jarvis_backend.py
import sys
sys.stdout.reconfigure(encoding="utf-8")

import asyncio
import json
import os
import threading
import queue
import webbrowser
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import socketserver # Import socketserver for ThreadingHTTPServer

import re

import pyttsx3
import websockets
from openai import OpenAI   # OpenAI client, but we'll point it to Gemini

# --- Configuration for Deployment ---
# Try to get the port from the environment variable provided by the hosting service
# If not found (e.g., running locally), default to a common port.
WEBSOCKET_PORT = int(os.environ.get("PORT", 5001))
HTTP_PORT = int(os.environ.get("HTTP_PORT", 8000)) # Define a separate env var for HTTP port

# Use "0.0.0.0" to listen on all available network interfaces
WEBSOCKET_HOST = "0.0.0.0"
HTTP_HOST = "0.0.0.0"


# ------------------------------- GEMINI SETUP -------------------------------

# PUT YOUR GEMINI API KEY HERE (do NOT commit the real key to public repos).
GEMINI_API_KEY = "AIzaSyBYdA6TT1zOpj4M6Dm9aFkGwPaSIiHEbFU" # Replace with your actual key or fetch from env variables

if not GEMINI_API_KEY or GEMINI_API_KEY == "PASTE_YOUR_GEMINI_API_KEY_HERE":
    print(
        "WARNING: GEMINI_API_KEY is not set correctly. "
        "Edit jarvis_backend.py and replace GEMINI_API_KEY with your actual key, or set it as an environment variable."
    )

# Create OpenAI-compatible client that talks to Gemini instead of OpenAI
client = (
    OpenAI(
        api_key=GEMINI_API_KEY,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    )
    if GEMINI_API_KEY and GEMINI_API_KEY != "PASTE_YOUR_GEMINI_API_KEY_HERE"
    else None
)

# ------------------------------- GLOBALS -------------------------------

commands = {
    "google": "https://google.com",
    "facebook": "https://facebook.com",
    "youtube": "https://youtube.com",
    "linkedin": "https://linkedin.com",
}

# TTS engine (speaks out loud on the PC running this script)
# NOTE: TTS might not work reliably in all cloud deployment environments.
# Browser-based TTS (in jarvis.html) is generally more reliable for end-users.
engine = pyttsx3.init()
engine.setProperty("rate", 150)
engine.setProperty("volume", 1.0)

voices = engine.getProperty("voices")
DEFAULT_VOICE_ID = engine.getProperty("voice")
EN_VOICE_ID = DEFAULT_VOICE_ID
HI_VOICE_ID = None

for v in voices:
    try:
        langs = []
        if hasattr(v, "languages") and v.languages:
            for lang in v.languages:
                if isinstance(lang, bytes):
                    lang = lang.decode("utf-8", errors="ignore")
                langs.append(lang.lower())
        name = getattr(v, "name", "").lower()
        vid = getattr(v, "id", "").lower()

        if any("hi" in l for l in langs) or "hindi" in name or "hi-" in vid:
            if HI_VOICE_ID is None:
                HI_VOICE_ID = v.id
        if any("en" in l for l in langs) or "english" in name or "en-" in vid:
            if EN_VOICE_ID == DEFAULT_VOICE_ID:
                EN_VOICE_ID = v.id
    except Exception:
        continue

print(f"TTS voices selected -> EN: {EN_VOICE_ID} | HI: {HI_VOICE_ID}")

DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")
HINDI_KEYWORDS = [
    "namaste", "namaskar", "shukriya", "dhanyavaad", "dhanyavad",
    "kya", "kaise", "nahi", "haan", "mera", "aap", "tum", "kyun", "kyon",
    "bahut", "krta", "karta", "raha", "rha", "kr", "hai", "hu",
]

def detect_language(text: str) -> str:
    """Very lightweight language detection: returns 'hi' or 'en'."""
    if not text:
        return "en"
    if DEVANAGARI_RE.search(text):
        return "hi"
    lower = text.lower()
    if any(kw in lower for kw in HINDI_KEYWORDS):
        return "hi"
    return "en"

def set_tts_language(lang_code: str):
    """Switch pyttsx3 voice between English and Hindi."""
    try:
        if lang_code == "hi" and HI_VOICE_ID is not None:
            engine.setProperty("voice", HI_VOICE_ID)
        else:
            engine.setProperty("voice", EN_VOICE_ID)
    except Exception as e:
        print(f"Error setting TTS voice: {e}")

# TTS worker thread + queue so speech is non-blocking and stoppable
tts_queue: "queue.Queue[str]" = queue.Queue()
tts_thread = None
tts_thread_stop = threading.Event()

def tts_worker():
    """Runs in a background thread. Takes text from tts_queue and plays it with pyttsx3."""
    print("TTS worker started.")
    while not tts_thread_stop.is_set():
        try:
            text = tts_queue.get(timeout=0.1)
        except queue.Empty:
            continue

        if text is None:  # Sentinel for shutdown
            break

        if not text:
            continue

        print(f"Jarvis (TTS): {text}")
        try:
            engine.say(text)
            engine.runAndWait()
        except Exception as e:
            print(f"TTS error: {e}")
    print("TTS worker stopped.")

def start_tts_thread():
    """Ensure the TTS worker thread is running."""
    global tts_thread
    if tts_thread is None or not tts_thread.is_alive():
        tts_thread_stop.clear()
        tts_thread = threading.Thread(target=tts_worker, daemon=True)
        tts_thread.start()

def stop_tts_thread():
    """Stop the TTS worker thread cleanly on program exit."""
    tts_thread_stop.set()
    try:
        tts_queue.put_nowait(None)
    except queue.Full:
        pass
    if tts_thread is not None:
        tts_thread.join(timeout=1.0)
    try:
        engine.stop()
    except Exception:
        pass

def clear_tts_queue():
    """Remove all pending texts from the TTS queue."""
    try:
        while True:
            tts_queue.get_nowait()
    except queue.Empty:
        pass

def stop_speaking():
    """Immediately stop current TTS output and clear anything queued."""
    clear_tts_queue()
    try:
        engine.stop()
    except Exception as e:
        print(f"Error stopping TTS: {e}")

def speak(text: str):
    """Queue text for speaking on the local machine. Non-blocking."""
    if not text:
        return
    start_tts_thread()
    try:
        tts_queue.put_nowait(text)
    except queue.Full:
        print("TTS queue full, dropping text:", text)

# ------------------------------- HELPER FUNCTIONS -------------------------------

def ask_gemini(prompt: str):
    """Ask Gemini and return the text reply."""
    if client is None:
        print("Gemini client is not configured (no valid GEMINI_API_KEY).")
        return None

    try:
        response = client.chat.completions.create(
            model="gemini-2.5-flash",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are Jarvis, a smart, concise and helpful AI assistant. "
                        "Answer clearly in a few sentences unless the user asks for detail. "
                        "Always respond in the same language as the user's message. "
                        "If the user uses Hindi, respond in natural Hindi using Devanagari script; "
                        "if they use English, respond in English."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        )

        msg = response.choices[0].message
        answer = msg.content if hasattr(msg, "content") else str(msg)
        return answer.strip() if answer else None

    except Exception as e:
        print(f"Gemini API Error: {e}")
        return None

def process_command(command: str) -> str:
    """Main Jarvis logic: handles commands, opens sites, or asks Gemini."""
    if not command:
        return "I didn't hear any command."

    raw_command = command.strip()
    if not raw_command:
        return "I didn't hear any command."

    lower = raw_command.lower()
    print(f"User command: {raw_command}")

    lang = detect_language(raw_command)
    set_tts_language(lang)

    stop_keywords = [
        "stop", "stop speaking", "stop talking", "be quiet", "shut up",
        "cancel", "terminate", "quit",
    ]
    if any(kw in lower for kw in stop_keywords):
        stop_speaking()
        return "Speech stopped."

    for keyword, url in commands.items():
        if f"open {keyword}" in lower:
            msg = f"Opening {keyword}."
            speak(msg)
            try:
                webbrowser.open(url)
            except Exception as e:
                print(f"Browser open error: {e}")
            return msg

    if any(x in lower for x in ["exit", "shutdown"]):
        stop_speaking()
        msg = "Shutting down."
        speak(msg)
        return "Shutting down system..."

    speak("Let me think.")
    ai_answer = ask_gemini(raw_command)

    if ai_answer:
        speak(ai_answer)
        return ai_answer

    fallback_msg = (
        "I'm having trouble reaching the Gemini AI service right now. "
        "Please check the GEMINI_API_KEY in this script and your internet connection."
    )
    speak(fallback_msg)
    return fallback_msg

# ------------------------------- WEBSOCKET SERVER -------------------------------

async def handle_connection(websocket):
    """Handle one WebSocket client (your frontend)."""
    print("Frontend connected ✔")

    try:
        async for message in websocket:
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                print(f"Invalid JSON from frontend: {message}")
                reply = "Invalid data format."
                await websocket.send(json.dumps({"reply": reply, "lang": "en"}))
                continue

            command = data.get("command", "")
            print(f"Received command from frontend: {command}")

            reply = process_command(command) or "No response"
            lang = detect_language(command)
            await websocket.send(json.dumps({"reply": reply, "lang": lang}))

    except websockets.exceptions.ConnectionClosed:
        print("Frontend disconnected ✖")

# ------------------------------- HTTP SERVER (SERVE jarvis.html) -------------------------------

class CustomHTTPRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        # Specify the directory to serve files from
        self.directory = os.path.dirname(os.path.abspath(__file__))
        super().__init__(*args, directory=self.directory, **kwargs)

def start_http_server():
    """Start a simple HTTP server in a background thread."""
    print(f"Serving static files from: {os.path.dirname(os.path.abspath(__file__))}")
    try:
        # Use ThreadingHTTPServer for better concurrency
        server = ThreadingHTTPServer((HTTP_HOST, HTTP_PORT), CustomHTTPRequestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        print(f"HTTP server running on http://{HTTP_HOST}:{HTTP_PORT}")
        return server
    except Exception as e:
        print(f"Error starting HTTP server: {e}")
        return None


# ------------------------------- MAIN APP LAUNCHER -------------------------------

async def main():
    start_tts_thread()
    http_server = start_http_server()

    # Automatically open the HTML UI in the default browser
    url = f"http://localhost:{HTTP_PORT}/jarvis.html" # Use localhost for initial open
    print(f"Opening browser at {url}")
    try:
        webbrowser.open(url)
    except Exception as e:
        print(f"Could not open browser automatically: {e}")
        print(f"Please open {url} manually.")

    try:
        # Use the determined port and host for the WebSocket server
        async with websockets.serve(handle_connection, WEBSOCKET_HOST, WEBSOCKET_PORT):
            print(f"WebSocket server running on ws://{WEBSOCKET_HOST}:{WEBSOCKET_PORT}")
            await asyncio.Future()  # Run forever
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"Error starting WebSocket server: {e}")
    finally:
        print("Shutting down HTTP server and TTS...")
        if http_server:
            try:
                http_server.shutdown()
            except Exception as e:
                print(f"Error shutting down HTTP server: {e}")
        stop_tts_thread()
        print("Shutdown complete.")


if __name__ == "__main__":
    try:
        # Ensure the script runs as an asyncio application
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nKeyboardInterrupt received. Exiting...")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
