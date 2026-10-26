"""Source AirPlay smoke test, coordinated with the shared Round 5 TV mutex."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from pathlib import Path

from gnome_adb_tv_remote.core.airplay import AirPlayClient
from gnome_adb_tv_remote.core.local_media import MediaSession


def main() -> None:
    """Wait for ownership, play both reference clips, then stop and release."""
    work = Path.home() / "tinytv-airplay-work"
    lock = work / "TV.lock"
    status = work / "status/probe-r5.md"
    if "COMPLETE" not in (work / "status/tester-r3.md").read_text():
        raise RuntimeError("Tester Round 3 is not COMPLETE")
    while True:
        try:
            with lock.open("x") as owner:
                owner.write("probe\n")
            break
        except FileExistsError:
            print("Waiting for TV.lock: " + lock.read_text().strip(), flush=True)
            time.sleep(60)
    results = []
    client = AirPlayClient("192.168.2.192", 7000)
    os.environ.setdefault("XDG_CACHE_HOME", str(work / "probe-cache"))
    try:
        with status.open("a") as report:
            report.write("\nPROBE USING TV\n")
        caps = client.capabilities()
        for name in ("PXL_20261025_182139252.mp4", "XiaoYing_Video_1790339249840 (2).mp4"):
            session = MediaSession()
            try:
                client.stop()
                started = time.monotonic()
                url = session.prepare(
                    Path.home() / "Videos/tv-receiver-check" / name,
                    client.host,
                    caps,
                    lambda text: print(text, flush=True),
                )
                ready_seconds = time.monotonic() - started
                client.play(url)
                observations = []
                for _ in range(12):
                    time.sleep(2)
                    observation = asdict(client.status())
                    observations.append(observation)
                    print(name, json.dumps(observation), flush=True)
                    if observation["error"]:
                        break
                advancing = any(
                    b["position"] > a["position"] for a, b in zip(observations, observations[1:])
                )
                success = (
                    advancing
                    and any(s["state"] == "PLAYING" for s in observations)
                    and not any(s["error"] for s in observations)
                )
                results.append(
                    {
                        "clip": name,
                        "ready_seconds": ready_seconds,
                        "url": url,
                        "position_advancing_no_error": success,
                        "observations": observations,
                        "visual_orientation": "Not verified on physical panel",
                    }
                )
                print("RESULT", name, success, flush=True)
            finally:
                try:
                    client.stop()
                finally:
                    session.close()
    except Exception as error:
        results.append({"error": str(error)})
        raise
    finally:
        (work / "status/probe-r5-e2e.json").write_text(json.dumps(results, indent=2))
        with status.open("a") as report:
            report.write("\nPROBE DONE WITH TV\n" + json.dumps(results, indent=2) + "\n")
        if lock.exists() and lock.read_text().strip() == "probe":
            lock.unlink()


if __name__ == "__main__":
    main()
