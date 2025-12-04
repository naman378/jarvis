import sys
sys.stdout.reconfigure(encoding="utf-8")

import asyncio
import json
import os
import threading
import queue
import webbrowser
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

import pyttsx3
import websockets
from openai import OpenAI   # OpenAI client, but we'll point it to Gemini

# ------------------------------- GEMINI SETUP -------------------------------

# PUT YOUR GEMINI API KEY HERE (do NOT commit the real key to public repos).
GEMINI_API_KEY = "AIzaSyBN9QD4fs609Fn3zsNty09NahZ1OCkqivo"

if not GEMINI_API_KEY or GEMINI_API_KEY == "PASTE_YOUR_GEMINI_API_KEY_HERE":
    print(
        "WARNING: GEMINI_API_KEY is not set correctly. "
        "Edit jarvis_backend.py and replace GEMINI_API_KEY with your actual key."
    )

# Create OpenAI-compatible client that talks to Gemini instead of OpenAI
# Docs: https://ai.google.dev/gemini-api/docs/openai
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
engine = pyttsx3.init()
engine.setProperty("rate", 150)
engine.setProperty("volume", 1.0)

# TTS worker thread + queue so speech is non-blocking and stoppable
tts_queue: "queue.Queue[str]" = queue.Queue()
tts_thread = None
tts_thread_stop = threading.Event()


def tts_worker():
    """
    Runs in a background thread.
    Takes text from tts_queue and plays it with pyttsx3.
    """
    print("TTS worker started.")
    while not tts_thread_stop.is_set():
        try:
            text = tts_queue.get(timeout=0.1)
        except queue.Empty:
            continue

        if text is None:
            # Sentinel for shutdown
            break

        if not text:
            continue

        print(f"Jarvis (TTS): {text}")
        try:
            engine.say(text)
            engine.runAndWait()
        except Exception as e:
            print("TTS error:", e)

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
    # Also stop any ongoing speech
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
    """
    Immediately stop current TTS output and clear anything queued.
    This is used for commands like 'stop', 'quit', 'terminate', etc.
    """
    clear_tts_queue()
    try:
        engine.stop()  # Interrupt current runAndWait in the worker thread
    except Exception as e:
        print("Error stopping TTS:", e)


def speak(text: str):
    """
    Queue text for speaking on the local machine.
    Non-blocking: just enqueues the text for the TTS worker thread.
    """
    if not text:
        return
    start_tts_thread()
    try:
        tts_queue.put_nowait(text)
    except queue.Full:
        print("TTS queue full, dropping text:", text)


# ------------------------------- HELPER FUNCTIONS -------------------------------

def ask_gemini(prompt: str):
    """
    Ask Gemini (via OpenAI-compatible API) and return the text reply.
    Returns None if there is any error (no API key, network issue, etc.).
    """
    if client is None:
        print("Gemini client is not configured (no valid GEMINI_API_KEY).")
        return None

    try:
        response = client.chat.completions.create(
            model="gemini-2.5-flash",   # any compatible Gemini model is fine
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are Jarvis, a smart, concise and helpful AI assistant. "
                        "Answer clearly in a few sentences unless the user asks for detail."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            # max_tokens=300,  # optional
        )

        msg = response.choices[0].message

        if hasattr(msg, "content"):
            answer = msg.content
        elif isinstance(msg, dict) and "content" in msg:
            answer = msg["content"]
        else:
            answer = str(msg)

        if answer:
            return answer.strip()
        return None

    except Exception as e:
        print("Gemini API Error:", e)
        return None


def process_command(command: str) -> str:
    """
    Main Jarvis logic:

    - If user says "stop / quit / terminate / be quiet / shut up / cancel",
      stop speaking immediately.
    - If user says "open google / youtube / facebook / linkedin", open those sites.
    - If user says "exit / shutdown", respond with shutdown message.
    - For every other sentence, send it to Gemini and speak the answer.
    """
    if not command:
        return "I didn't hear any command."

    raw_command = command.strip()
    if not raw_command:
        return "I didn't hear any command."

    lower = raw_command.lower()
    print(f"User command: {raw_command}")

    # ----------------- Stop current speech commands -----------------
    stop_keywords = [
        "stop",
        "stop speaking",
        "stop talking",
        "be quiet",
        "shut up",
        "cancel",
        "terminate",
        "quit",
    ]
    if any(kw in lower for kw in stop_keywords):
        stop_speaking()
        # Do NOT speak this back out loud (only text to frontend).
        return "Speech stopped."

    # ----------------- Open website commands -----------------
    for keyword, url in commands.items():
        if f"open {keyword}" in lower:
            msg = f"Opening {keyword}."
            speak(msg)
            try:
                webbrowser.open(url)
            except Exception as e:
                print("Browser open error:", e)
            return msg

    # ----------------- Exit / shutdown -----------------
    if any(x in lower for x in ["exit", "shutdown"]):
        # Stop any ongoing speech first
        stop_speaking()
        msg = "Shutting down."
        speak(msg)
        return "Shutting down system..."

    # ----------------- Default: ask Gemini for everything else -----------------
    speak("Let me think.")
    ai_answer = ask_gemini(raw_command)

    if ai_answer:
        speak(ai_answer)
        return ai_answer

    # If we reach here, Gemini failed (no key, network issue, etc.)
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
                print("Invalid JSON from frontend:", message)
                reply = "Invalid data format."
                await websocket.send(json.dumps({"reply": reply}))
                continue

            command = data.get("command", "")
            print("Received command from frontend:", command)

            reply = process_command(command) or "No response"
            await websocket.send(json.dumps({"reply": reply}))

    except websockets.exceptions.ConnectionClosed:
        print("Frontend disconnected ✖")


# ------------------------------- HTTP SERVER (SERVE jarvis.html) -------------------------------

def start_http_server():
    """
    Start a simple HTTP server in a background thread to serve jarvis.html
    and other static files from the current folder.
    """
    web_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(web_dir)

    server = ThreadingHTTPServer(("localhost", 8000), SimpleHTTPRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print("HTTP server running on http://localhost:8000")
    return server


# ------------------------------- MAIN APP LAUNCHER -------------------------------

async def main():
    start_tts_thread()
    http_server = start_http_server()

    # Automatically open the HTML UI in the default browser
    url = "http://localhost:8000/jarvis.html"
    print(f"Opening browser at {url}")
    try:
        webbrowser.open(url)
    except Exception as e:
        print("Could not open browser automatically:", e)
        print(f"Please open {url} manually.")

    try:
        async with websockets.serve(handle_connection, "localhost", 5001):
            print("WebSocket server running on ws://localhost:5001")
            # Run forever
            await asyncio.Future()
    except asyncio.CancelledError:
        pass
    finally:
        print("Shutting down HTTP server and TTS...")
        try:
            http_server.shutdown()
        except Exception:
            pass
        stop_tts_thread()
        print("Shutdown complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nKeyboardInterrupt received. Exiting...")