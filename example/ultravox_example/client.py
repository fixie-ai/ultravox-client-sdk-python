import argparse
import asyncio
import logging
import signal
import sys

import ultravox_client as uv


async def main():
    session = uv.UltravoxSession(
        experimental_messages=set(args.experimental_messages.split(","))
        if args.experimental_messages
        else None
    )
    last_transcript = None
    done = asyncio.Event()

    @session.on("status")
    def on_status():
        status = session.status
        logging.info(f"status: {status}")
        if status == uv.UltravoxSessionStatus.LISTENING:
            # Prompt the user to make it clear they're expected to speak next.
            print("User:  ", end="\r")
        elif status == uv.UltravoxSessionStatus.DISCONNECTED:
            done.set()

    @session.on("transcripts")
    def on_transcript():
        def transcript_to_str(transcript):
            return f"{'User' if transcript.speaker == 'user' else 'Agent'}:  {transcript.text}"

        nonlocal last_transcript
        transcript = session.transcripts[-1]
        if last_transcript and last_transcript.speaker != transcript.speaker:
            print(transcript_to_str(last_transcript), end="\n")
            last_transcript = None
        next_print = transcript_to_str(transcript)
        last_print = transcript_to_str(last_transcript) if last_transcript else ""
        display_text = next_print + " " * max(0, len(last_print) - len(next_print))
        last_transcript = transcript if not transcript.final else None
        print(display_text, end="\n" if transcript.final else "\r")

    @session.on("experimental_message")
    def on_experimental_message(message):
        logging.info(f"Received experimental message: {message}")

    @session.on("error")
    def on_error(error):
        logging.exception("Client error", exc_info=error)
        done.set()

    await session.join_call(args.join_url)
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, lambda: done.set())
    loop.add_signal_handler(signal.SIGTERM, lambda: done.set())
    await done.wait()
    await session.leave_call()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="client.py")
    parser.add_argument(
        "--join-url",
        type=str,
        required=True,
        help="Join URL for a call. Will join the call (like a normal client) if provided.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Show verbose session information"
    )
    parser.add_argument(
        "--very-verbose", "-vv", action="store_true", help="Show debug logs too"
    )
    parser.add_argument(
        "--experimental-messages",
        type=str,
        help="Comma-separated list of experimental messages to enable (e.g. 'debug')",
    )

    args = parser.parse_args()
    if args.very_verbose:
        logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)
    elif args.verbose:
        logging.basicConfig(stream=sys.stdout, level=logging.INFO)
    if not args.very_verbose:
        logging.getLogger("livekit").disabled = True

    asyncio.run(main())
